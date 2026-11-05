"""Telegram: белый список, меню, команды, кнопки. Настоящий aiogram, фейковый Bot API по HTTP."""

from __future__ import annotations

import json

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from sqlalchemy import func, select

from pai.bot import texts
from pai.bot.app import AppContext, build_dispatcher
from pai.db.models import AuditEvent, Turn
from tests.conftest import OWNER, STRANGER
from tests.fakes import FakeTelegram


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def harness(tg, assistant, settings, sessions):
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(settings, assistant, sessions))

    async def send(push) -> list[str]:
        before = len(tg.calls)
        push()
        await dp.feed_raw_update(bot, tg.updates.get_nowait())
        if assistant.memory is not None:
            await assistant.memory.wait()  # фоновая обработка памяти
        return [p.get("text", "") for m, p in tg.calls[before:] if m in ("sendMessage", "editMessageText")]

    yield send, tg, dp, bot
    await bot.session.close()


def last_markup(tg: FakeTelegram) -> dict:
    for _method, params in reversed(tg.calls):
        if "reply_markup" in params:
            return json.loads(params["reply_markup"])
    return {}


async def test_stranger_is_denied_without_any_processing(harness, script, sessions) -> None:
    send, tg, *_ = harness
    out = await send(lambda: tg.push_text(STRANGER, "привет, бот"))
    assert out == [texts.ACCESS_DENIED]
    assert await send(lambda: tg.push_text(STRANGER, "ну ответь")) == []  # отказ не чаще раза в час
    assert await send(lambda: tg.push_text(STRANGER, "/start")) == []
    tg.push_callback(STRANGER, "model:gpt-6.1-sol")
    await harness[2].feed_raw_update(harness[3], tg.updates.get_nowait())
    assert script.seen == []  # модель не вызывалась — денег не потрачено
    async with sessions() as s:
        kinds = (await s.scalars(select(AuditEvent.kind))).all()
        turns = await s.scalar(select(func.count()).select_from(Turn))
    assert kinds == ["access_denied"] and turns == 0


async def test_start_shows_menu_keyboard(harness) -> None:
    send, tg, *_ = harness
    out = await send(lambda: tg.push_text(OWNER, "/start"))
    assert "личный AI-ассистент" in out[0]
    keyboard = last_markup(tg)["keyboard"]
    labels = [b["text"] for row in keyboard for b in row]
    assert labels == [texts.BTN_NEW, texts.BTN_MEMORY, texts.BTN_MODEL, texts.BTN_VOICE,
                      texts.BTN_SETTINGS, texts.BTN_STATS, texts.BTN_HELP]


async def test_chat_with_typing_and_context(harness, script) -> None:
    send, tg, *_ = harness
    script.replies = ["Привет!", "Помню: джаз."]
    assert await send(lambda: tg.push_text(OWNER, "привет, люблю джаз")) == ["Привет!"]
    assert await send(lambda: tg.push_text(OWNER, "что я люблю?")) == ["Помню: джаз."]
    assert any(m == "sendChatAction" and p["action"] == "typing" for m, p in tg.calls)
    assert script.seen[-1].user_prompts == ["привет, люблю джаз", "что я люблю?"]


async def test_menu_buttons_work_as_commands(harness, script) -> None:
    send, tg, *_ = harness
    await send(lambda: tg.push_text(OWNER, "старая тема"))
    assert await send(lambda: tg.push_text(OWNER, texts.BTN_NEW)) == [texts.NEW_CONVERSATION]
    await send(lambda: tg.push_text(OWNER, "новая тема"))
    assert script.seen[-1].user_prompts == ["новая тема"]
    assert "Что я умею сейчас" in (await send(lambda: tg.push_text(OWNER, texts.BTN_HELP)))[0]
    stats = (await send(lambda: tg.push_text(OWNER, texts.BTN_STATS)))[0]
    # 2 ответа в чате + 1 обработка памяти после «Новый диалог» (модель памяти — gpt-6-luna).
    assert "Этот месяц" in stats and "gpt-6-luna: 3" in stats and "оценкой" not in stats
    assert len(script.seen) == 2  # кнопки меню не уходят в модель


async def test_model_selection_by_button(harness, assistant, script) -> None:
    send, tg, *_ = harness
    out = await send(lambda: tg.push_text(OWNER, texts.BTN_MODEL))
    assert "GPT-6 Luna" in out[0]
    buttons = [b["callback_data"] for row in last_markup(tg)["inline_keyboard"] for b in row]
    assert "model:gpt-6.1-sol" in buttons and not any("claude" in b for b in buttons)  # нет ключа
    out = await send(lambda: tg.push_callback(OWNER, "model:gpt-6.1-sol"))
    assert texts.model_set("GPT-6.1 Sol") in out
    user = await assistant.get_user(OWNER)
    assert assistant.current_model(user).id == "gpt-6.1-sol"
    out = await send(lambda: tg.push_callback(OWNER, "model:claude-sonnet-5-5"))  # подделанная кнопка
    assert assistant.current_model(await assistant.get_user(OWNER)).id == "gpt-6.1-sol"


async def test_settings_style_and_instructions(harness, assistant, script) -> None:
    send, tg, *_ = harness
    out = await send(lambda: tg.push_text(OWNER, texts.BTN_SETTINGS))
    assert "Стиль ответов: обычно" in out[0]
    out = await send(lambda: tg.push_callback(OWNER, "style:short"))
    assert "Стиль ответов: коротко" in out[0]
    assert await send(lambda: tg.push_callback(OWNER, "instr:edit")) == [texts.INSTRUCTIONS_PROMPT]
    assert await send(lambda: tg.push_text(OWNER, "Обращайся на ты")) == [texts.INSTRUCTIONS_SAVED]
    assert script.seen == []  # текст инструкций не ушёл в модель как вопрос
    await send(lambda: tg.push_text(OWNER, "привет"))
    assert "Обращайся на ты" in script.seen[-1].instructions
    await send(lambda: tg.push_callback(OWNER, "instr:clear"))
    assert (await assistant.get_user(OWNER)).instructions is None


async def test_cancel_instructions_editing(harness, script) -> None:
    send, tg, *_ = harness
    await send(lambda: tg.push_callback(OWNER, "instr:edit"))
    assert await send(lambda: tg.push_text(OWNER, "/cancel")) == [texts.CANCELLED]
    await send(lambda: tg.push_text(OWNER, "обычный вопрос"))
    assert script.seen[-1].user_prompts[-1] == "обычный вопрос"


async def test_voice_files_and_unknown(harness, script) -> None:
    send, tg, *_ = harness
    voice = {"voice": {"file_id": "v", "file_unique_id": "u", "duration": 3}}
    assert await send(lambda: tg.push_raw_message(OWNER, voice)) == [texts.VOICE_DISABLED]  # без движка голоса
    doc = {"document": {"file_id": "d", "file_unique_id": "u2", "file_name": "a.pdf"}}
    assert await send(lambda: tg.push_raw_message(OWNER, doc)) == [texts.NOT_YET_FILES]
    assert await send(lambda: tg.push_text(OWNER, "/abracadabra")) == [texts.UNKNOWN_COMMAND]
    assert script.seen == []


async def test_group_messages_are_ignored(harness, script) -> None:
    send, tg, *_ = harness
    assert await send(lambda: tg.push_raw_message(OWNER, {"text": "привет всем"}, chat_type="group")) == []
    assert script.seen == []


async def test_duplicate_update_processed_once(harness, script) -> None:
    send, tg, dp, bot = harness
    tg.push_text(OWNER, "привет")
    update = tg.updates.get_nowait()
    await dp.feed_raw_update(bot, update)
    await dp.feed_raw_update(bot, update)
    assert len(script.seen) == 1


async def test_long_answer_is_split(harness, script) -> None:
    send, tg, *_ = harness
    script.replies = ["Очень длинная мысль. " * 500]
    out = await send(lambda: tg.push_text(OWNER, "расскажи"))
    assert len(out) >= 2 and all(len(p) <= 4096 for p in out)


async def test_handler_crash_shows_friendly_error_and_no_text_in_logs(harness, assistant, monkeypatch, caplog) -> None:
    import logging

    from pai.logging_setup import SafeFormatter

    send, tg, *_ = harness

    async def boom(user, text):
        raise RuntimeError(f"упало на: {text}")

    monkeypatch.setattr(assistant, "ask", boom)
    caplog.set_level(logging.ERROR)
    assert await send(lambda: tg.push_text(OWNER, "мой секретный план")) == [texts.INTERNAL_ERROR]
    formatted = "\n".join(SafeFormatter("%(message)s").format(r) for r in caplog.records)
    assert "RuntimeError" in formatted and "секретный план" not in formatted


# ---- 🧠 Память ------------------------------------------------------------------------------

async def test_memory_overview_list_edit_delete(harness, assistant) -> None:
    send, tg, *_ = harness
    user = await assistant.get_user(OWNER)
    store = assistant.memory.store
    a = await store.add(user.id, "preference", "Любит короткие ответы")
    b = await store.add(user.id, "project", "Лендинг для кофейни")
    out = (await send(lambda: tg.push_text(OWNER, texts.BTN_MEMORY)))[0]
    assert "Сохранено записей: 2" in out and "Предпочтения: 1" in out and "Проекты: 1" in out
    listing = (await send(lambda: tg.push_callback(OWNER, "mem:list:0")))[0]
    assert f"#{a.id} [Предпочтения] Любит короткие ответы" in listing
    buttons = [b2["callback_data"] for row in last_markup(tg)["inline_keyboard"] for b2 in row]
    assert f"mem:del:{b.id}" in buttons and f"mem:edit:{a.id}" in buttons

    assert texts.memory_edit_prompt("Любит короткие ответы") in await send(lambda: tg.push_callback(OWNER, f"mem:edit:{a.id}"))
    assert await send(lambda: tg.push_text(OWNER, "Любит ответы без воды")) == [texts.MEMORY_EDITED]
    edited = await store.get(user.id, a.id)
    assert edited.content == "Любит ответы без воды" and edited.source == "user" and edited.confidence == 1.0

    tg.push_callback(OWNER, f"mem:del:{b.id}")
    await harness[2].feed_raw_update(harness[3], tg.updates.get_nowait())
    assert [m.id for m in await store.active(user.id)] == [a.id]


async def test_memory_search_export_and_remember(harness, assistant, script) -> None:
    send, tg, *_ = harness
    assert await send(lambda: tg.push_text(OWNER, "/remember Мой день рождения 12 марта")) == [texts.REMEMBERED]
    assert await send(lambda: tg.push_text(OWNER, "/remember пароль: qwerty")) == [texts.REMEMBER_REJECTED]
    await send(lambda: tg.push_callback(OWNER, "mem:search"))
    found = (await send(lambda: tg.push_text(OWNER, "рождении")))[0]
    assert "12 марта" in found
    assert script.seen == []  # поиск не уходит в модель
    await send(lambda: tg.push_callback(OWNER, "mem:export"))
    method, params = next((m, p) for m, p in reversed(tg.calls) if m == "sendDocument")
    assert method == "sendDocument"


async def test_memory_clear_and_full_wipe_need_confirmation(harness, assistant, sessions) -> None:
    send, tg, *_ = harness
    await send(lambda: tg.push_text(OWNER, "привет"))
    user = await assistant.get_user(OWNER)
    await assistant.memory.store.add(user.id, "fact", "Любит джаз")
    assert await send(lambda: tg.push_callback(OWNER, "mem:clear")) == [texts.MEMORY_CLEAR_CONFIRM]
    assert await send(lambda: tg.push_callback(OWNER, "mem:cancel")) == [texts.CANCELLED]
    assert len(await assistant.memory.store.active(user.id)) == 1
    await send(lambda: tg.push_callback(OWNER, "mem:clear:yes"))
    assert await assistant.memory.store.active(user.id) == []
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Turn)) == 1  # переписка осталась

    assert await send(lambda: tg.push_callback(OWNER, "mem:wipe")) == [texts.WIPE_CONFIRM]
    assert await send(lambda: tg.push_callback(OWNER, "mem:wipe:yes")) == [texts.WIPED]
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Turn)) == 0
        assert await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.tg_id == OWNER)) == 0


@pytest.fixture
async def two_users(tg, make_assistant, settings, sessions):
    s = settings.model_copy(update={"allowed_telegram_ids": [OWNER, OWNER + 1]})
    a = make_assistant(settings_override={"allowed_telegram_ids": [OWNER, OWNER + 1]})
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(s, a, sessions))
    yield a, dp, bot
    await bot.session.close()


async def test_one_user_cannot_touch_anothers_memory(two_users, tg) -> None:
    a, dp, bot = two_users
    owner = await a.get_user(OWNER)
    secret = await a.memory.store.add(owner.id, "fact", "Секрет владельца")
    for data in (f"mem:del:{secret.id}", f"mem:edit:{secret.id}", "mem:wipe:yes", "mem:clear:yes"):
        tg.push_callback(OWNER + 1, data)
        await dp.feed_raw_update(bot, tg.updates.get_nowait())
    assert [m.content for m in await a.memory.store.active(owner.id)] == ["Секрет владельца"]
    tg.push_callback(OWNER + 1, "mem:list:0")
    await dp.feed_raw_update(bot, tg.updates.get_nowait())
    assert "Секрет владельца" not in " ".join(tg.sent_texts())
