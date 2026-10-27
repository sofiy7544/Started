"""Сценарии характера с поддельной моделью.

ВАЖНО: эти тесты проверяют, что модель получает нужный контекст и указания (история,
память, подсказки против повторов, правила честности). Качество самих ответов они НЕ
проверяют — для этого нужен настоящий API: scripts/live_check.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion.llm.client import MemoryExtraction, NewFact
from companion.persona import CORE_RULES
from companion.repositories import memory
from tests.conftest import new_user
from tests.fakes import FakeLLM


async def say(service, user, text: str) -> None:
    await service.handle_message(user, text)
    await service.wait_background()


def user_block(call) -> str:
    return call.system[1]["text"]


async def test_short_greeting_at_first_contact(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await say(service, user, "привет")
    call = llm.chat_calls[-1]
    assert [m["content"] for m in call.messages] == ["привет"]
    assert "Это начало вашего общения" in user_block(call)
    assert "мало знакомы" in user_block(call)  # не делать вид, что знает собеседника


async def test_ongoing_chat_does_not_greet_again(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = ["привет! как ты?", "о, расскажи"]
    await say(service, user, "привет")
    await say(service, user, "сегодня был странный день")
    assert "не здоровайся заново" in user_block(llm.chat_calls[-1])


async def test_return_after_long_pause_allows_greeting(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await say(service, user, "пока, до завтра")
    await pool.execute("UPDATE messages SET created_at = created_at - interval '2 days' WHERE user_id = $1", user.id)
    await say(service, user, "я вернулся")
    assert "после перерыва" in user_block(llm.chat_calls[-1])


async def test_several_facts_in_one_message_reach_long_term_memory(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.extractions = [MemoryExtraction(
        summary="Познакомились: Саша, дизайнер, любит джаз, ждёт собеседования.",
        preferred_name="Саша",
        new_facts=[NewFact(category="fact", text="Работает дизайнером"),
                   NewFact(category="interest", text="Любит джаз"),
                   NewFact(category="thread", text="Завтра собеседование")],
    )]
    await say(service, user, "я Саша, дизайнер, обожаю джаз, а завтра у меня собеседование")
    await service.reset_context(user)  # прежний диалог сразу сжимается в память
    await service.wait_background()
    await say(service, user, "ну что, как думаешь, всё пройдёт хорошо?")
    block = user_block(llm.chat_calls[-1])
    for expected in ("обращаться к нему: Саша", "Работает дизайнером", "Любит джаз", "Завтра собеседование"):
        assert expected in block
    assert "Незавершённые темы" in block


async def test_repeated_question_keeps_previous_answer_in_view(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = ["люблю джаз, особенно старый", "ага", "я же говорила — джаз 🙂"]
    await say(service, user, "какую музыку ты любишь?")
    await say(service, user, "понятно")
    await say(service, user, "какую музыку ты любишь?")
    call = llm.chat_calls[-1]
    contents = [m["content"] for m in call.messages]
    assert contents.count("какую музыку ты любишь?") == 2
    assert "люблю джаз, особенно старый" in contents  # модель видит, что уже отвечала
    assert "Начни по-другому" in user_block(call)
    assert "Не повторяй собственные фразы" in call.system[0]["text"]


async def test_are_you_real_question_has_honesty_rules(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await say(service, user, "ты настоящая?")
    persona_prompt = llm.chat_calls[-1].system[0]["text"]
    assert CORE_RULES in persona_prompt
    assert "ты виртуальная собеседница на основе ИИ" in persona_prompt
    assert "Не утверждай, что ты реальный человек" in persona_prompt


async def test_no_question_hint_after_question_streak(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = ["как прошёл день?", "а что было на работе?"]
    await say(service, user, "привет")
    await say(service, user, "нормально")
    await say(service, user, "устал просто")
    assert "без вопроса" in user_block(llm.chat_calls[-1])


async def test_long_conversation_drops_old_messages_but_keeps_summary(pool, make_service) -> None:
    llm = FakeLLM()
    llm.extractions = [MemoryExtraction(summary="В начале обсуждали поход в горы.",
                                        new_facts=[NewFact(category="interest", text="Любит горы")])]
    llm.extractions += [MemoryExtraction(summary=f"Часть {i}.") for i in range(10)]
    service = make_service(llm_override=llm)
    user = await new_user(pool, 1)
    await say(service, user, "давай расскажу про поход в горы")
    for i in range(20):
        await say(service, user, f"реплика {i}")
    call = llm.chat_calls[-1]
    contents = [m["content"] for m in call.messages]
    assert "давай расскажу про поход в горы" not in contents  # старое вышло из окна
    assert len(call.messages) <= 10
    block = user_block(call)
    # Факт из начала разговора сохраняется навсегда (до /forget)...
    assert "Любит горы" in block
    # ...а в контекст идут только 4 последних резюме: самые старые выпадают (задокументировано).
    assert block.count("Часть ") <= 4
    assert "В начале обсуждали поход в горы." not in block


async def test_profile_name_is_not_presented_as_fact(pool, service, llm) -> None:
    user = await new_user(pool, 1, name="xX_Dark_Xx")
    await say(service, user, "привет")
    assert "может быть не настоящим именем" in user_block(llm.chat_calls[-1])
    assert await memory.list_facts(pool, user.id) == []


def test_time_in_context_is_moscow_time() -> None:
    from companion.llm.prompts import build_user_context
    from companion.repositories.memory import StoredMessage

    t = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)
    ctx = build_user_context(first_name=None, facts=[], summaries=[],
                             history=[StoredMessage(1, "user", "hi", t)],
                             previous_message=StoredMessage(0, "assistant", "x", t - timedelta(minutes=1)), now=t)
    assert "около 23:00 (МСК)" in ctx
