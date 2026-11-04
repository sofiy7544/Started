"""Сценарий ответа: настоящий pydantic-ai, подставная модель, настоящая SQLite."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic_ai import exceptions as E
from sqlalchemy import func, select

from pai.bot import texts
from pai.db import engine as db
from pai.db.models import AuditEvent, Turn, UsageRecord
from pai.services.assistant import Outcome
from tests.conftest import OWNER, Script


async def total_cost(sessions) -> Decimal:
    async with sessions() as s:
        return Decimal(str(await s.scalar(select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)))))


async def test_dialog_keeps_context(assistant, script) -> None:
    user = await assistant.get_user(OWNER, "Аня")
    script.replies = ["Привет, Аня!", "Ты говорила, что любишь джаз.", "ok"]
    await assistant.ask(user, "привет, я Аня")
    await assistant.ask(user, "я люблю джаз")
    third = script.seen[-1]
    assert third.user_prompts == ["привет, я Аня", "я люблю джаз"]
    assert third.assistant_texts == ["Привет, Аня!"]
    answer = await assistant.ask(user, "что я люблю?")
    assert answer.outcome is Outcome.OK and answer.text == "ok"
    assert script.seen[-1].user_prompts[-1] == "что я люблю?"


async def test_instructions_contain_base_style_and_personal(assistant, script) -> None:
    user = await assistant.get_user(OWNER)
    await assistant.set_reply_style(user, "short")
    await assistant.set_instructions(user, "Обращайся на ты. Я маркетолог.")
    await assistant.ask(user, "привет")
    instr = script.seen[-1].instructions
    assert "личный AI-ассистент" in instr  # из config/assistant.md
    assert "максимально коротко" in instr
    assert "Я маркетолог" in instr
    assert "04.11.2026" in instr  # дата для понимания «сегодня/завтра»


async def test_new_conversation_drops_context_but_keeps_archive(assistant, script, sessions) -> None:
    user = await assistant.get_user(OWNER)
    await assistant.ask(user, "старая тема")
    await assistant.new_conversation(user)
    await assistant.ask(user, "новая тема")
    assert script.seen[-1].user_prompts == ["новая тема"]
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Turn)) == 2  # архив цел


async def test_history_limits_turns_and_chars(make_assistant, script) -> None:
    a = make_assistant(settings_override={"max_history_turns": 3, "max_history_chars": 1000})
    user = await a.get_user(OWNER)
    for i in range(6):
        await a.ask(user, f"сообщение {i}")
    assert len(script.seen[-1].user_prompts) == 4  # 3 прошлых + текущее
    big = make_assistant(settings_override={"max_history_turns": 50, "max_history_chars": 1000})
    await big.new_conversation(user)
    for i in range(5):
        await big.ask(user, f"{i} " + "x" * 400)
    assert len(script.seen[-1].user_prompts) <= 3  # по символам влезает 2 прошлых обмена


async def test_long_message_is_truncated(make_assistant, script) -> None:
    a = make_assistant(settings_override={"max_user_message_chars": 100})
    user = await a.get_user(OWNER)
    await a.ask(user, "я" * 5000)
    prompt = script.seen[-1].user_prompts[-1]
    assert len(prompt) < 200 and "обрезано" in prompt


async def test_reply_size_limit_is_passed(make_assistant, script) -> None:
    seen_settings = {}

    async def fn(messages, info):
        seen_settings.update(info.model_settings or {})
        return await Script().fn(messages, info)

    from pydantic_ai.models.function import FunctionModel

    from pai.llm.engine import PydanticAIEngine
    a = make_assistant(settings_override={"max_reply_tokens": 777, "llm_max_retries": 1})
    a._engine = PydanticAIEngine(a._settings, model_factory=lambda m: FunctionModel(fn, model_name="gpt-6-luna"))
    await a.ask(await a.get_user(OWNER), "привет")
    assert seen_settings["max_tokens"] == 777 and seen_settings["max_retries"] == 1


async def test_model_switching(assistant, script) -> None:
    user = await assistant.get_user(OWNER)
    assert assistant.current_model(user).id == "gpt-6-luna"
    assert await assistant.set_model(user, "gpt-6.1-sol") is not None
    await assistant.ask(user, "привет")
    assert script.seen[-1].model_name is not None
    again = await assistant.get_user(OWNER)
    assert assistant.current_model(again).id == "gpt-6.1-sol"  # сохранено в БД
    assert await assistant.set_model(user, "claude-sonnet-5-5") is None  # нет ключа Anthropic


async def test_usage_cost_is_recorded(assistant, script, sessions) -> None:
    user = await assistant.get_user(OWNER)
    answer = await assistant.ask(user, "привет")
    # FunctionModel: 1000 вход, 100 выход; цена gpt-6-luna $0.10/$0.50 за 1M.
    assert answer.cost_usd == Decimal("0.00015")
    async with sessions() as s:
        rec = await s.scalar(select(UsageRecord))
    assert rec.model_id == "gpt-6-luna" and rec.confirmed and rec.cost_usd == Decimal("0.00015")
    assert assistant.budget.reserved == 0


async def test_budget_exhausted_and_kill_switch(make_assistant, script, sessions) -> None:
    a = make_assistant(settings_override={"monthly_budget_usd": 0.0001})
    user = await a.get_user(OWNER)
    ans = await a.ask(user, "привет")
    assert ans.outcome is Outcome.BUDGET and ans.text == texts.BUDGET_EXHAUSTED
    assert script.seen == []
    off = make_assistant(settings_override={"paid_apis_disabled": True})
    ans = await off.ask(user, "привет")
    assert ans.outcome is Outcome.BUDGET and ans.text == texts.PAID_DISABLED and script.seen == []


async def test_free_local_model_works_with_kill_switch(make_assistant, script) -> None:
    a = make_assistant(settings_override={"paid_apis_disabled": True,
                                          "ollama_base_url": "http://localhost:11434/v1"})
    user = await a.get_user(OWNER)
    await a.set_model(user, "ollama-local")
    ans = await a.ask(user, "привет")
    assert ans.outcome is Outcome.OK and ans.cost_usd == 0


async def test_concurrent_requests_cannot_overshoot_budget(make_assistant, sessions) -> None:
    slow = Script()

    async def slow_fn(messages, info):
        await asyncio.sleep(0.2)
        return await slow.fn(messages, info)

    from pydantic_ai.models.function import FunctionModel

    from pai.llm.engine import PydanticAIEngine
    a = make_assistant(settings_override={"monthly_budget_usd": 0.0025, "max_reply_tokens": 2000})
    a._engine = PydanticAIEngine(a._settings, model_factory=lambda m: FunctionModel(slow_fn, model_name="gpt-6-luna"))
    # Оценка вызова ≈ $0.001+ (2000 токенов ответа × $0.50/1M + вход): в $0.0025 влезают две.
    users = [await a.get_user(OWNER + i) for i in range(6)]
    answers = await asyncio.gather(*(a.ask(u, "привет") for u in users))
    outcomes = [x.outcome for x in answers]
    assert outcomes.count(Outcome.OK) == 2 and outcomes.count(Outcome.BUDGET) == 4
    assert await total_cost(sessions) <= Decimal("0.0025")
    assert a.budget.reserved == 0


@pytest.mark.parametrize("exc,expected", [
    (E.ModelAuthenticationError("m", "x"), texts.ERR_AUTH),
    (E.ModelQuotaExceededError("m", "x"), texts.ERR_QUOTA),
    (E.ModelRateLimitError("m", "x"), texts.ERR_RATE_LIMIT),
    (E.ModelOverloadedError("m", "x"), texts.ERR_TEMPORARY),
    (E.ModelNotFoundError("m", "x"), texts.ERR_MODEL_NOT_FOUND),
    (E.ModelContextWindowExceededError("m", "x"), texts.ERR_CONTEXT_TOO_LONG),
])
async def test_api_errors_give_clear_messages(assistant, script, sessions, exc, expected) -> None:
    script.replies = [exc]
    ans = await assistant.ask(await assistant.get_user(OWNER), "привет")
    assert ans.outcome is Outcome.FAILED and ans.text == expected
    assert await total_cost(sessions) == 0  # ответа с usage не было — расход не пишется
    assert assistant.budget.reserved == 0
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Turn)) == 0
        assert await s.scalar(select(AuditEvent.kind).where(AuditEvent.kind == "llm_error")) == "llm_error"


async def test_timeout_is_accounted_by_upper_estimate(make_assistant, script, sessions) -> None:
    a = make_assistant(settings_override={"llm_max_retries": 2})
    script.replies = [E.ModelTimeoutError("m", "timeout")]
    ans = await a.ask(await a.get_user(OWNER), "привет")
    assert ans.text == texts.ERR_TIMEOUT
    async with sessions() as s:
        rec = await s.scalar(select(UsageRecord))
    assert not rec.confirmed and rec.cost_usd > 0  # оценка × 3 попытки
    assert a.budget.reserved == 0


async def test_breaker_pauses_after_repeated_failures(make_assistant, script, clock) -> None:
    a = make_assistant(settings_override={"breaker_failure_threshold": 2})
    user = await a.get_user(OWNER)
    script.replies = [E.ModelOverloadedError("m", "x"), E.ModelOverloadedError("m", "x")]
    await a.ask(user, "1")
    await a.ask(user, "2")
    ans = await a.ask(user, "3")
    assert ans.outcome is Outcome.UNAVAILABLE and len(script.seen) == 2  # третий запрос не ушёл
    clock.t += 61
    assert (await a.ask(user, "4")).outcome is Outcome.OK


async def test_invalid_key_pauses_longer(make_assistant, script, clock) -> None:
    a = make_assistant()
    user = await a.get_user(OWNER)
    script.replies = [E.ModelAuthenticationError("m", "bad")]
    await a.ask(user, "1")
    clock.t += 61
    assert (await a.ask(user, "2")).outcome is Outcome.UNAVAILABLE
    assert len(script.seen) == 1


async def test_rate_limit(make_assistant, script, clock) -> None:
    a = make_assistant(settings_override={"messages_per_minute": 2})
    user = await a.get_user(OWNER)
    await a.ask(user, "1")
    await a.ask(user, "2")
    r3, r4 = await a.ask(user, "3"), await a.ask(user, "4")
    assert r3.outcome is Outcome.RATE_LIMITED and r3.text == texts.TOO_FAST
    assert r4.text is None and len(script.seen) == 2


async def test_restart_restores_context(make_assistant, script, db_url) -> None:
    a1 = make_assistant()
    await a1.ask(await a1.get_user(OWNER), "меня зовут Петя")
    engine2 = db.make_engine(db_url)  # «перезапуск»: новое подключение и новый ассистент
    try:
        a2 = make_assistant(sessions_override=db.make_sessionmaker(engine2))
        await a2.ask(await a2.get_user(OWNER), "как меня зовут?")
        assert script.seen[-1].user_prompts == ["меня зовут Петя", "как меня зовут?"]
    finally:
        await engine2.dispose()


async def test_users_do_not_see_each_other(assistant, script) -> None:
    a = await assistant.get_user(OWNER)
    b = await assistant.get_user(OWNER + 1)
    await assistant.ask(a, "секрет Алисы")
    await assistant.ask(b, "привет")
    assert script.seen[-1].user_prompts == ["привет"]


async def test_concurrent_first_messages_create_one_user(assistant, sessions) -> None:
    """Регрессия из живого запуска: два апдейта нового пользователя пришли одновременно."""
    from pai.db.models import User

    users = await asyncio.gather(*(assistant.get_user(OWNER, "Аня") for _ in range(5)))
    assert len({u.id for u in users}) == 1
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 1
