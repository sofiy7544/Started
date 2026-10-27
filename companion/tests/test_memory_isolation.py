"""Долгосрочная память, /reset, удаление данных и изоляция пользователей."""

from __future__ import annotations

import asyncio

from companion.llm.client import LLMError, LLMErrorKind, MemoryExtraction, NewFact
from companion.repositories import memory
from companion.services.chat import ReplyKind
from tests.conftest import new_user
from tests.fakes import FakeLLM


def extraction(summary="Поговорили.", name=None, facts=(), obsolete=()) -> MemoryExtraction:
    return MemoryExtraction(
        summary=summary, preferred_name=name,
        new_facts=[NewFact(category=c, text=t) for c, t in facts],
        obsolete_fact_ids=list(obsolete),
    )


async def chat_n(service, user, n: int, prefix: str = "сообщение") -> None:
    for i in range(n):
        assert (await service.handle_message(user, f"{prefix} {i}")).kind is ReplyKind.OK
        await service.wait_background()


async def test_summarization_saves_facts_and_compacts_context(pool, service, llm) -> None:
    # settings: keep_recent=4, threshold=8 → после 4 обменов (8 сообщений) старое сжимается.
    user = await new_user(pool, 1)
    llm.extractions = [extraction(
        "Петя рассказал про гитару.", name="Петя",
        facts=[("interest", "Играет на гитаре"), ("thread", "Готовится к концерту в субботу")],
    )]
    await chat_n(service, user, 4)
    assert len(llm.memory_calls) == 1
    facts = await memory.list_facts(pool, user.id)
    assert {(f.category, f.text) for f in facts} == {
        ("name", "Петя"), ("interest", "Играет на гитаре"),
        ("thread", "Готовится к концерту в субботу"),
    }
    remaining = await memory.context_messages(pool, user.id, 0, 100)
    assert len(remaining) == 4  # в контексте только свежий хвост
    await service.handle_message(user, "как дела?")
    sent = llm.chat_calls[-1].everything
    assert "Петя рассказал про гитару." in sent and "Играет на гитаре" in sent
    assert "обращаться к нему: Петя" in sent


async def test_random_phrases_and_secrets_are_not_stored_as_facts(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.extractions = [extraction(
        "Был разговор, карта 4111 1111 1111 1111.",
        facts=[("fact", "Пароль от почты: qwerty123"), ("fact", "Номер карты 4111 1111 1111 1111"),
               ("fact", "Живёт по адресу Ленина 5"), ("fact", "x" * 500), ("interest", "Любит джаз")],
    )]
    await chat_n(service, user, 4)
    assert [f.text for f in await memory.list_facts(pool, user.id)] == ["Любит джаз"]
    summary = (await memory.recent_summaries(pool, user.id, 5))[0]
    assert "4111" not in summary


async def test_contradictions_replace_old_facts(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await memory.add_facts(pool, user.id, [("fact", "Курит"), ("fact", "Живёт в Казани")],
                           max_facts=60, max_threads=5)
    await memory.set_preferred_name(pool, user.id, "Александр")
    old = {f.text: f.id for f in await memory.list_facts(pool, user.id)}
    llm.extractions = [extraction(
        name="Саша", facts=[("fact", "Бросил курить")], obsolete=[old["Курит"]],
    )]
    await chat_n(service, user, 4)
    facts = {(f.category, f.text) for f in await memory.list_facts(pool, user.id)}
    assert facts == {("name", "Саша"), ("fact", "Живёт в Казани"), ("fact", "Бросил курить")}


async def test_memory_limits_evict_oldest(pool) -> None:
    user = await new_user(pool, 1)
    threads = [("thread", f"тема {i}") for i in range(4)]
    await memory.add_facts(pool, user.id, threads, max_facts=60, max_threads=2)
    left = [f.text for f in await memory.list_facts(pool, user.id) if f.category == "thread"]
    assert left == ["тема 2", "тема 3"]
    await memory.add_facts(pool, user.id, [("fact", f"факт {i}") for i in range(5)], max_facts=3, max_threads=2)
    assert len(await memory.list_facts(pool, user.id)) == 3


async def test_failed_extraction_retries_later_without_loops(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.extractions = [LLMError(LLMErrorKind.TEMPORARY, "x")]
    await chat_n(service, user, 4)
    assert len(llm.memory_calls) == 1  # одна попытка, без циклов повторов
    assert len(await memory.context_messages(pool, user.id, 0, 100)) == 8  # ничего не потеряно
    await chat_n(service, user, 1)
    assert len(llm.memory_calls) == 2
    assert len(await memory.context_messages(pool, user.id, 0, 100)) == 4


async def test_reset_starts_fresh_dialog_but_keeps_memory(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    other = await new_user(pool, 2)
    await memory.add_facts(pool, user.id, [("interest", "Любит джаз")], max_facts=60, max_threads=5)
    await chat_n(service, other, 2, "чужое")
    await chat_n(service, user, 2, "старое")
    llm.extractions = [extraction("Обсуждали старое.")]
    await service.reset_context(user)
    await service.wait_background()
    assert len(llm.memory_calls) == 1  # прежний диалог сжат в резюме
    assert "старое 0" in llm.memory_calls[0] and "чужое" not in llm.memory_calls[0]

    await service.handle_message(user, "новое начало")
    call = llm.chat_calls[-1]
    assert [m["content"] for m in call.messages] == ["новое начало"]
    assert "Любит джаз" in call.everything and "Обсуждали старое." in call.everything
    # чужой диалог не тронут
    assert len(await memory.context_messages(pool, other.id, 0, 100)) == 4


async def test_forget_memory_and_delete_user_touch_only_own_data(pool, service, llm) -> None:
    alice = await new_user(pool, 1)
    bob = await new_user(pool, 2)
    for u in (alice, bob):
        await chat_n(service, u, 2)
        await memory.add_facts(pool, u.id, [("interest", f"интерес {u.id}")], max_facts=60, max_threads=5)
        await memory.add_summary(pool, u.id, f"резюме {u.id}", 1, 2)

    await service.forget_memory(alice)
    snap = await service.memory_snapshot(alice)
    assert (snap.facts, snap.summaries_count, snap.messages_count) == ([], 0, 0)
    assert (await service.find_user(1)).age_confirmed  # аккаунт и подтверждение 18+ остались

    bob_snap = await service.memory_snapshot(bob)
    assert len(bob_snap.facts) == 1 and bob_snap.summaries_count == 1 and bob_snap.messages_count == 4

    await service.delete_user(bob)
    assert await service.find_user(2) is None
    for table in ("messages", "memory_facts", "conversation_summaries"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE user_id = $1", bob.id) == 0
    # учёт расходов остаётся, но без привязки к человеку
    assert await pool.fetchval("SELECT count(*) FROM api_usage WHERE user_id = $1", bob.id) == 0
    assert await pool.fetchval("SELECT count(*) FROM api_usage WHERE user_id IS NULL") == 2
    assert (await service.find_user(1)) is not None


async def test_model_cannot_delete_other_users_facts(pool, service, llm) -> None:
    alice = await new_user(pool, 1)
    bob = await new_user(pool, 2)
    await memory.add_facts(pool, bob.id, [("fact", "Факт Боба")], max_facts=60, max_threads=5)
    bob_fact = (await memory.list_facts(pool, bob.id))[0]
    llm.extractions = [extraction(obsolete=[bob_fact.id, 999_999])]
    await chat_n(service, alice, 4)
    assert [f.text for f in await memory.list_facts(pool, bob.id)] == ["Факт Боба"]
    assert await memory.remove_facts(pool, alice.id, [bob_fact.id]) == 0


async def test_parallel_dialogs_do_not_leak_between_users(pool, make_service) -> None:
    """Два пользователя пишут одновременно: в запрос каждого попадает только его переписка."""
    def reply(call) -> str:
        last = call.messages[-1]["content"]
        return f"эхо: {last}"

    llm = FakeLLM(reply_fn=reply, delay=0.01)
    service = make_service(llm_override=llm)
    alice = await new_user(pool, 1, "Алиса")
    bob = await new_user(pool, 2, "Боб")
    await memory.add_facts(pool, alice.id, [("fact", "Секрет Алисы: любит оперу")], max_facts=60, max_threads=5)
    await memory.add_facts(pool, bob.id, [("fact", "Секрет Боба: боится пауков")], max_facts=60, max_threads=5)
    await memory.add_summary(pool, alice.id, "Резюме Алисы", 1, 1)
    await memory.add_summary(pool, bob.id, "Резюме Боба", 1, 1)
    llm.extractions = [extraction(f"резюме {i}") for i in range(20)]

    async def talk(user, tag: str) -> None:
        for i in range(10):
            await service.handle_message(user, f"{tag}-{i}")
            await asyncio.sleep(0)

    await asyncio.gather(talk(alice, "alice"), talk(bob, "bob"))
    await service.wait_background()

    for call in llm.chat_calls:
        text = call.everything
        if "alice-" in call.messages[-1]["content"]:
            assert "bob-" not in text and "Боб" not in text and "пауков" not in text
            assert "Алиса" in text
        else:
            assert "alice-" not in text and "Алис" not in text and "оперу" not in text
            assert "Боб" in text
    for transcript in llm.memory_calls:
        assert not ("alice-" in transcript and "bob-" in transcript)
    a_msgs = await memory.context_messages(pool, alice.id, 0, 100)
    assert all("bob" not in m.content for m in a_msgs)


class SlowExtractLLM(FakeLLM):
    """Модель «думает» над памятью долго — чтобы /forget успел прийти во время сжатия."""

    started = 0

    async def extract_memory(self, **kw):
        self.started += 1
        await asyncio.sleep(0.3)
        return await super().extract_memory(**kw)


async def test_forget_during_summarization_does_not_resurrect_facts(pool, make_service) -> None:
    llm = SlowExtractLLM()
    llm.extractions = [extraction("Резюме удалённого.", name="Петя", facts=[("interest", "Любит джаз")])]
    service = make_service(llm_override=llm)
    user = await new_user(pool, 1)
    for i in range(4):
        await service.handle_message(user, f"сообщение {i}")
    await asyncio.sleep(0.1)
    assert llm.started == 1  # сжатие уже идёт (ждёт модель)
    await service.forget_memory(user)
    await service.wait_background()
    snap = await service.memory_snapshot(user)
    assert (snap.facts, snap.summaries_count, snap.messages_count) == ([], 0, 0)


async def test_delete_during_summarization_leaves_nothing(pool, make_service) -> None:
    llm = SlowExtractLLM()
    llm.extractions = [extraction("Резюме.", facts=[("interest", "Любит джаз")])]
    service = make_service(llm_override=llm)
    user = await new_user(pool, 1)
    for i in range(4):
        await service.handle_message(user, f"сообщение {i}")
    await asyncio.sleep(0.1)
    assert llm.started == 1
    await service.delete_user(user)
    await service.wait_background()
    for table in ("users", "messages", "memory_facts", "conversation_summaries"):
        col = "id" if table == "users" else "user_id"
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE {col} = $1", user.id) == 0
