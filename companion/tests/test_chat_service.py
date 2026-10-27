"""ChatService на реальном PostgreSQL с поддельной моделью."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from companion import texts
from companion.llm.client import LLMError, LLMErrorKind, LLMResponse, Usage
from companion.repositories import memory, usage, users
from companion.services.chat import ReplyKind
from tests.conftest import new_user
from tests.fakes import FakeLLM


async def test_new_user_is_created_and_needs_age_confirmation(pool, service) -> None:
    user = await service.get_or_create_user(555, "Аня", "ru")
    assert user.id > 0 and not user.age_confirmed
    again = await service.get_or_create_user(555, "Аня", "ru")
    assert again.id == user.id  # повторный /start не плодит пользователей
    await service.confirm_age(user)
    assert (await service.find_user(555)).age_confirmed


async def test_multi_turn_dialog_uses_history(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = ["привет! я Мира", "о, джаз — это здорово", "конечно помню"]
    for text in ("привет", "люблю джаз", "помнишь, что я люблю?"):
        reply = await service.handle_message(user, text)
        assert reply.kind is ReplyKind.OK
    last = llm.chat_calls[-1]
    assert [m["role"] for m in last.messages] == ["user", "assistant"] * 2 + ["user"]
    assert last.messages[2]["content"] == "люблю джаз"
    assert last.messages[-1]["content"] == "помнишь, что я люблю?"
    assert last.system[0]["cache_control"] == {"type": "ephemeral"}  # общий промпт кэшируется
    stored = await memory.context_messages(pool, user.id, 0, 100)
    assert [m.content for m in stored][-1] == "конечно помню"


async def test_context_and_reply_size_are_limited(pool, make_service, llm) -> None:
    service = make_service(settings_override={
        "max_context_messages": 6, "summarize_threshold": 6, "keep_recent_messages": 2,
        "max_context_chars": 500, "max_reply_tokens": 321, "max_user_message_chars": 100,
    })
    user = await new_user(pool, 1)
    llm.extractions = [None] * 10
    for i in range(10):
        await service.handle_message(user, f"сообщение {i} " + "х" * 50)
        await service.wait_background()
    call = llm.chat_calls[-1]
    assert call.max_tokens == 321
    assert len(call.messages) <= 6
    assert sum(len(m["content"]) for m in call.messages) <= 500
    await service.handle_message(user, "я" * 5000)
    assert len(llm.chat_calls[-1].messages[-1]["content"]) < 150
    assert "обрезано" in llm.chat_calls[-1].messages[-1]["content"]


async def test_truncated_reply_is_cut_at_sentence(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = [LLMResponse("Это целое предложение. А это обрыва", "m", "m", Usage(1, 1), truncated=True)]
    reply = await service.handle_message(user, "расскажи")
    assert reply.text == "Это целое предложение."


async def test_sensitive_data_is_masked_before_storage_and_model(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await service.handle_message(user, "вот моя карта 4111 1111 1111 1111 и код 123456")
    stored = (await memory.context_messages(pool, user.id, 0, 10))[0].content
    assert "4111" not in stored and "123456" not in stored
    assert "4111" not in llm.chat_calls[-1].everything


async def test_temporary_api_error_gives_friendly_text_and_breaker_stops_calls(
    pool, make_service, llm, clock
) -> None:
    service = make_service(settings_override={"breaker_failure_threshold": 2})
    user = await new_user(pool, 1)
    llm.replies = [LLMError(LLMErrorKind.TEMPORARY, "overloaded")] * 2
    r1 = await service.handle_message(user, "раз")
    r2 = await service.handle_message(user, "два")
    assert r1.text == texts.TEMPORARY_ERROR and r2.kind is ReplyKind.TEMPORARY_ERROR
    r3 = await service.handle_message(user, "три")
    assert r3.kind is ReplyKind.UNAVAILABLE
    assert len(llm.chat_calls) == 2  # предохранитель не пустил третий запрос
    clock.t += 61
    r4 = await service.handle_message(user, "четыре")
    assert r4.kind is ReplyKind.OK
    assert len(llm.chat_calls) == 3


async def test_invalid_api_key_is_reported_as_unavailable(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = [LLMError(LLMErrorKind.AUTH, "AuthenticationError status=401")]
    reply = await service.handle_message(user, "привет")
    assert reply.kind is ReplyKind.UNAVAILABLE and reply.text == texts.UNAVAILABLE
    assert (await service.handle_message(user, "ау")).kind is ReplyKind.UNAVAILABLE
    assert len(llm.chat_calls) == 1  # с неверным ключом API больше не дёргаем


async def test_refusal_returns_neutral_text_and_is_not_saved(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = [LLMResponse("", "m", "m", Usage(10, 0), refused=True)]
    reply = await service.handle_message(user, "что-то")
    assert reply.kind is ReplyKind.REFUSED and reply.text == texts.REFUSED
    roles = [m.role for m in await memory.context_messages(pool, user.id, 0, 10)]
    assert roles == ["user"]


async def test_rate_limit_per_minute(pool, make_service, llm, clock) -> None:
    service = make_service(settings_override={"messages_per_minute": 2})
    user = await new_user(pool, 1)
    assert (await service.handle_message(user, "1")).kind is ReplyKind.OK
    assert (await service.handle_message(user, "2")).kind is ReplyKind.OK
    r3 = await service.handle_message(user, "3")
    r4 = await service.handle_message(user, "4")
    assert r3.kind is ReplyKind.RATE_LIMITED and r3.text == texts.TOO_FAST
    assert r4.kind is ReplyKind.RATE_LIMITED and r4.text is None  # без спама предупреждениями
    assert len(llm.chat_calls) == 2
    other = await new_user(pool, 2)
    assert (await service.handle_message(other, "привет")).kind is ReplyKind.OK
    clock.t += 61
    assert (await service.handle_message(user, "5")).kind is ReplyKind.OK


async def test_daily_limit(pool, make_service, llm) -> None:
    service = make_service(settings_override={"daily_messages_per_user": 2})
    user = await new_user(pool, 1)
    await service.handle_message(user, "1")
    await service.handle_message(user, "2")
    reply = await service.handle_message(user, "3")
    assert reply.kind is ReplyKind.DAILY_LIMIT and len(llm.chat_calls) == 2


async def test_burst_of_messages_gets_one_reply(pool, service, llm) -> None:
    """Несколько сообщений подряд, пока модель думает, — один ответ на всю пачку."""
    user = await new_user(pool, 1)
    llm.delay = 0.2
    first = asyncio.create_task(service.handle_message(user, "смотри"))
    await asyncio.sleep(0.05)
    rest = [asyncio.create_task(service.handle_message(user, t)) for t in ("что", "я нашёл")]
    results = await asyncio.gather(first, *rest)
    kinds = [r.kind for r in results]
    assert kinds == [ReplyKind.OK, ReplyKind.SKIPPED, ReplyKind.OK]
    assert len(llm.chat_calls) == 2
    sent = [m["content"] for m in llm.chat_calls[-1].messages]
    assert sent == ["смотри", "ответ 1", "что", "я нашёл"]  # ответ стоит за своим вопросом


async def test_budget_exhausted_blocks_calls(pool, make_service, llm) -> None:
    service = make_service(settings_override={"anthropic_monthly_budget_usd": 1.0})
    user = await new_user(pool, 1)
    await usage.record(pool, provider="anthropic", model="x", operation="chat", user_id=None,
                       cost_usd=Decimal("1.0"))
    reply = await service.handle_message(user, "привет")
    assert reply.kind is ReplyKind.BUDGET and reply.text == texts.BUDGET_EXHAUSTED
    assert llm.chat_calls == []


async def test_budget_blocks_call_that_would_exceed_limit(pool, make_service, llm) -> None:
    service = make_service(settings_override={
        "anthropic_monthly_budget_usd": 0.001, "chat_model": "claude-sonnet-5-5",
        "budget_soft_ratio": 1.0, "max_reply_tokens": 1000,
    })
    user = await new_user(pool, 1)
    # 1000 токенов ответа Sonnet = $0.01 > $0.001 — вызов не делается вовсе.
    assert (await service.handle_message(user, "привет")).kind is ReplyKind.BUDGET
    assert llm.chat_calls == []


async def test_soft_budget_switches_to_cheap_model_and_usage_is_recorded(pool, make_service, llm) -> None:
    service = make_service(settings_override={
        "anthropic_monthly_budget_usd": 10.0, "chat_model": "claude-sonnet-5-5",
        "memory_model": "claude-haiku-5-5",
    })
    user = await new_user(pool, 1)
    await service.handle_message(user, "привет")
    assert llm.chat_calls[-1].model == "claude-sonnet-5-5"
    await usage.record(pool, provider="anthropic", model="x", operation="chat", user_id=None,
                       cost_usd=Decimal("8.5"))
    await service.handle_message(user, "ещё")
    assert llm.chat_calls[-1].model == "claude-haiku-5-5"
    rows = await pool.fetch("SELECT model, operation, user_id, cost_usd FROM api_usage WHERE user_id IS NOT NULL")
    assert len(rows) == 2 and all(r["cost_usd"] > 0 for r in rows)


async def test_paid_apis_kill_switch(pool, make_service, llm) -> None:
    service = make_service(settings_override={"paid_apis_disabled": True})
    user = await new_user(pool, 1)
    assert (await service.handle_message(user, "привет")).kind is ReplyKind.BUDGET
    assert llm.chat_calls == []


async def test_user_deleted_during_generation(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.delay = 0.1
    task = asyncio.create_task(service.handle_message(user, "привет"))
    await asyncio.sleep(0.02)
    await users.delete(pool, user.id)  # в обход блокировки — худший случай
    assert (await task).kind is ReplyKind.SKIPPED
    # расход всё равно учтён — без привязки к удалённому пользователю
    assert await pool.fetchval("SELECT count(*) FROM api_usage WHERE user_id IS NULL") == 1


async def test_restart_restores_context(pool, make_service, settings) -> None:
    """Новый экземпляр сервиса (как после перезапуска) видит историю и факты."""
    llm1 = FakeLLM(replies=["рада знакомству, Петя"])
    s1 = make_service(llm_override=llm1)
    user = await new_user(pool, 7)
    await s1.handle_message(user, "меня зовут Петя")
    await memory.add_facts(pool, user.id, [("interest", "Играет на гитаре")], max_facts=10, max_threads=5)

    from companion import db
    from tests.conftest import TEST_DB
    pool2 = await db.create_pool(TEST_DB)
    try:
        llm2 = FakeLLM()
        s2 = make_service(llm_override=llm2, pool_override=pool2)
        user_after = await s2.find_user(7)
        await s2.handle_message(user_after, "что ты обо мне помнишь?")
        call = llm2.chat_calls[0]
        assert "меня зовут Петя" in [m["content"] for m in call.messages]
        assert "рада знакомству, Петя" in [m["content"] for m in call.messages]
        assert "Играет на гитаре" in call.everything
    finally:
        await pool2.close()


async def test_logs_do_not_contain_message_text(pool, service, llm, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    user = await new_user(pool, 1)
    llm.replies = ["секретный ответ модели", LLMError(LLMErrorKind.TEMPORARY, "x")]
    await service.handle_message(user, "очень личное сообщение")
    await service.handle_message(user, "ещё одно личное")
    await service.wait_background()
    assert "llm usage" in caplog.text  # расходы логируются
    for secret in ("очень личное", "ещё одно личное", "секретный ответ"):
        assert secret not in caplog.text
