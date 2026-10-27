"""Команды и сценарии Telegram: настоящий aiogram Dispatcher/Bot, фейковый Bot API по HTTP."""

from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from companion import texts
from companion.bot.app import AppContext, build_dispatcher
from companion.bot.handlers.commands import (
    AGE_OK,
    FORGET_ALL,
    FORGET_CANCEL,
    FORGET_MEMORY,
)
from companion.repositories import memory
from tests.fakes import FakeTelegram

USER = 111
OTHER = 222


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def harness(tg, pool, service, settings, persona):
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(settings, persona, service), pool)

    async def send(update_factory) -> list[str]:
        before = len(tg.calls)
        update_factory()
        await dp.feed_raw_update(bot, tg.updates.get_nowait())
        await service.wait_background()
        return [p["text"] for m, p in tg.calls[before:] if m in ("sendMessage", "editMessageText")]

    yield send, tg, dp, bot
    await bot.session.close()


async def test_start_shows_ai_disclosure_and_age_gate(harness, persona) -> None:
    send, tg, *_ = harness
    out = await send(lambda: tg.push_text(USER, "/start"))
    assert len(out) == 1
    assert "AI-персонаж" in out[0] and "не реальный человек" in out[0] and "18" in out[0]
    markup = [p for m, p in tg.calls if m == "sendMessage"][-1]["reply_markup"]
    assert AGE_OK in markup

    # до подтверждения модель не вызывается
    out = await send(lambda: tg.push_text(USER, "привет"))
    assert out == [texts.AGE_REQUIRED]

    out = await send(lambda: tg.push_callback(USER, AGE_OK))
    assert out == [texts.after_confirm(persona)]
    assert any(m == "editMessageReplyMarkup" for m, _ in tg.calls)

    out = await send(lambda: tg.push_text(USER, "/start"))
    assert out == [texts.welcome_back(persona)]  # повторный /start без повторного гейта


async def test_dialog_with_typing_indicator(harness, llm) -> None:
    send, tg, *_ = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))
    llm.replies = ["привет! как прошёл день?", "звучит утомительно"]
    assert await send(lambda: tg.push_text(USER, "привет")) == ["привет! как прошёл день?"]
    assert await send(lambda: tg.push_text(USER, "весь день на работе")) == ["звучит утомительно"]
    actions = [p for m, p in tg.calls if m == "sendChatAction"]
    assert actions and actions[0]["action"] == "typing"
    assert len(llm.chat_calls[-1].messages) == 3


async def test_help_privacy_and_unknown_command(harness, persona) -> None:
    send, tg, *_ = harness
    help_out = await send(lambda: tg.push_text(USER, "/help"))
    for cmd in ("/reset", "/memory", "/forget", "/privacy"):
        assert cmd in help_out[0]
    privacy = await send(lambda: tg.push_text(USER, "/privacy"))
    assert "Claude API" in privacy[0] and "/forget" in privacy[0]
    assert await send(lambda: tg.push_text(USER, "/abracadabra")) == ["Такой команды нет. Список — /help."]


async def test_memory_reset_and_forget_flow(harness, pool, service, llm) -> None:
    send, tg, *_ = harness
    for uid in (USER, OTHER):
        await send(lambda uid=uid: tg.push_text(uid, "/start"))
        await send(lambda uid=uid: tg.push_callback(uid, AGE_OK))
        await send(lambda uid=uid: tg.push_text(uid, "привет"))
    me = await service.find_user(USER)
    other = await service.find_user(OTHER)
    await memory.add_facts(pool, me.id, [("interest", "Любит джаз")], max_facts=60, max_threads=5)
    await memory.add_facts(pool, other.id, [("interest", "Любит рок")], max_facts=60, max_threads=5)

    shown = (await send(lambda: tg.push_text(USER, "/memory")))[0]
    assert "Любит джаз" in shown and "Любит рок" not in shown

    assert await send(lambda: tg.push_text(USER, "/reset")) == [texts.RESET_DONE]
    await send(lambda: tg.push_text(USER, "новая тема"))
    assert [m["content"] for m in llm.chat_calls[-1].messages] == ["новая тема"]
    assert "Любит джаз" in llm.chat_calls[-1].everything

    # /forget → выбор → отмена: ничего не удалено
    assert await send(lambda: tg.push_text(USER, "/forget")) == [texts.FORGET_MENU]
    assert await send(lambda: tg.push_callback(USER, FORGET_MEMORY)) == [texts.FORGET_MEMORY_CONFIRM]
    assert await send(lambda: tg.push_callback(USER, FORGET_CANCEL)) == [texts.CANCELLED]
    assert len(await memory.list_facts(pool, me.id)) == 1

    # подтверждённое стирание памяти
    await send(lambda: tg.push_callback(USER, FORGET_MEMORY))
    assert await send(lambda: tg.push_callback(USER, FORGET_MEMORY + ":yes")) == [texts.FORGET_MEMORY_DONE]
    assert await memory.list_facts(pool, me.id) == []
    assert (await service.find_user(USER)).age_confirmed

    # полное удаление
    await send(lambda: tg.push_callback(USER, FORGET_ALL))
    assert await send(lambda: tg.push_callback(USER, FORGET_ALL + ":yes")) == [texts.FORGET_ALL_DONE]
    assert await service.find_user(USER) is None
    # другой пользователь не затронут
    assert [f.text for f in await memory.list_facts(pool, other.id)] == ["Любит рок"]
    assert (await service.memory_snapshot(other)).messages_count == 2
    # после удаления снова нужен /start
    assert await send(lambda: tg.push_text(USER, "ау")) == [texts.AGE_REQUIRED]


async def test_duplicate_update_is_processed_once(harness, llm) -> None:
    send, tg, dp, bot = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))
    tg.push_text(USER, "привет")
    update = tg.updates.get_nowait()
    await dp.feed_raw_update(bot, update)
    await dp.feed_raw_update(bot, update)  # Telegram прислал тот же update_id повторно
    assert len(llm.chat_calls) == 1


async def test_long_reply_is_split(harness, llm) -> None:
    send, tg, *_ = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))
    llm.replies = ["Очень длинная мысль. " * 400]
    out = await send(lambda: tg.push_text(USER, "расскажи сказку"))
    assert len(out) >= 2 and all(len(p) <= 4096 for p in out)


async def test_non_text_and_group_messages(harness, llm) -> None:
    send, tg, dp, bot = harness
    sticker = {"update_id": 900, "message": {
        "message_id": 1, "date": 1, "chat": {"id": USER, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "Т"},
        "sticker": {"file_id": "f", "file_unique_id": "u", "type": "regular", "width": 1,
                    "height": 1, "is_animated": False, "is_video": False}}}
    await dp.feed_raw_update(bot, sticker)
    assert tg.sent_texts() == [texts.ONLY_TEXT]
    group = {"update_id": 901, "message": {
        "message_id": 2, "date": 1, "chat": {"id": -100, "type": "group", "title": "g"},
        "from": {"id": USER, "is_bot": False, "first_name": "Т"}, "text": "привет всем"}}
    await dp.feed_raw_update(bot, group)
    assert tg.sent_texts() == [texts.ONLY_TEXT]  # в группах бот молчит
    assert llm.chat_calls == []


async def test_api_failure_message_reaches_user(harness, llm) -> None:
    from companion.llm.client import LLMError, LLMErrorKind

    send, tg, *_ = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))
    llm.replies = [LLMError(LLMErrorKind.TEMPORARY, "529")]
    assert await send(lambda: tg.push_text(USER, "привет")) == [texts.TEMPORARY_ERROR]


async def test_handler_crash_logs_no_user_text(harness, service, monkeypatch, caplog) -> None:
    import logging

    from companion.logging_setup import SafeFormatter

    send, tg, *_ = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))

    async def boom(user, text):
        raise RuntimeError(f"упало на тексте: {text}")

    monkeypatch.setattr(service, "handle_message", boom)
    caplog.set_level(logging.ERROR)
    out = await send(lambda: tg.push_text(USER, "мой домашний адрес секрет"))
    assert out == [texts.INTERNAL_ERROR]  # пользователь видит понятное сообщение, не traceback
    formatted = "\n".join(SafeFormatter("%(message)s").format(r) for r in caplog.records)
    assert "RuntimeError" in formatted
    assert "домашний адрес" not in formatted


async def test_other_user_cannot_delete_or_read_via_buttons(harness, pool, service) -> None:
    """Callback с чужого аккаунта (даже на сообщение в чате USER) действует только на свои данные."""
    send, tg, dp, bot = harness
    await send(lambda: tg.push_text(USER, "/start"))
    await send(lambda: tg.push_callback(USER, AGE_OK))
    await send(lambda: tg.push_text(USER, "привет"))
    me = await service.find_user(USER)
    await memory.add_facts(pool, me.id, [("interest", "Любит джаз")], max_facts=60, max_threads=5)

    for data in (FORGET_ALL + ":yes", FORGET_MEMORY + ":yes"):
        tg.push_callback(OTHER, data, message_id=1)
        update = tg.updates.get_nowait()
        update["callback_query"]["message"]["chat"]["id"] = USER  # «чужое» сообщение
        await dp.feed_raw_update(bot, update)
    await service.wait_background()
    assert await service.find_user(USER) is not None
    assert [f.text for f in await memory.list_facts(pool, me.id)] == ["Любит джаз"]
    assert (await service.memory_snapshot(me)).messages_count == 2

    shown = await send(lambda: tg.push_text(OTHER, "/memory"))
    assert "Любит джаз" not in shown[0]
