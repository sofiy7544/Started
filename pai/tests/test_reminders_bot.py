"""Напоминания и инициатива в Telegram: карточки, команды, кнопки, настройки, реальная отправка
через aiogram (фейковый Bot API)."""

from __future__ import annotations

import json

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from pai.bot import texts
from pai.bot.app import AppContext, build_dispatcher
from pai.bot.sender import TelegramSender
from pai.reminders.delivery import ReminderDelivery
from pai.reminders.service import ReminderService
from tests.conftest import CLOCK, OWNER
from tests.fakes import FakeTelegram


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def rbot(tg, assistant, settings, sessions):
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(settings, assistant, sessions))

    async def send(push) -> list[tuple[str, dict]]:
        before = len(tg.calls)
        push()
        await dp.feed_raw_update(bot, tg.updates.get_nowait())
        await assistant.memory.wait()
        return tg.calls[before:]

    yield send, tg, bot
    await bot.session.close()


def texts_of(calls) -> list[str]:
    return [p.get("text", "") for m, p in calls if m in ("sendMessage", "editMessageText")]


def buttons(calls) -> set[str]:
    out = set()
    for _m, p in calls:
        if "reply_markup" in p:
            for row in json.loads(p["reply_markup"]).get("inline_keyboard", []):
                out |= {b["callback_data"] for b in row}
    return out


async def test_natural_request_gives_code_made_confirmation_card(rbot, script) -> None:
    send, tg, _ = rbot
    script.replies = [{"tool": "create_reminder", "args": {"text": "позвонить врачу", "at_local": "2026-11-05 10:00"}},
                      "Хорошо!"]
    calls = await send(lambda: tg.push_text(OWNER, "Напомни мне завтра в 10:00 позвонить врачу"))
    out = texts_of(calls)
    assert out[0] == "Хорошо!"
    assert out[1].startswith("✅ Напоминание #") and "завтра в 10:00" in out[1] and "позвонить врачу" in out[1]
    assert {b for b in buttons(calls) if b.startswith("rem:")} == {"rem:edit:1", "rem:del:1"}


async def test_model_saying_done_without_tool_shows_no_card(rbot, script) -> None:
    send, tg, _ = rbot
    script.replies = ["Конечно, напомню!"]  # модель «пообещала», но инструмент не вызвала
    out = texts_of(await send(lambda: tg.push_text(OWNER, "напомни про звонок")))
    assert out == ["Конечно, напомню!"]  # карточки нет — видно, что напоминание не создано


async def test_commands_list_add_delete(rbot, assistant, script) -> None:
    send, tg, _ = rbot
    assert texts_of(await send(lambda: tg.push_text(OWNER, "/reminders"))) == [texts.REMINDERS_EMPTY]
    user = await assistant.get_user(OWNER)
    a = await assistant.reminders.create(user, "проверить оплату", in_minutes=120)
    b = await assistant.reminders.create(user, "отчёты", at_local="2026-11-09 09:00", repeat="weekly")
    calls = await send(lambda: tg.push_text(OWNER, texts.BTN_REMINDERS))
    listing = texts_of(calls)[0]
    assert f"#{a.id}" in listing and "каждую неделю" in listing and f"rem:del:{b.id}" in buttons(calls)
    assert texts_of(await send(lambda: tg.push_text(OWNER, f"/delete_reminder {a.id}"))) == \
        [texts.REMINDER_DELETED.format(id=a.id)]
    assert texts_of(await send(lambda: tg.push_text(OWNER, "/delete_reminder 999"))) == [texts.REMINDER_NOT_FOUND]
    script.replies = [{"tool": "create_reminder", "args": {"text": "купить хлеб", "in_minutes": 60}}, "Ок"]
    await send(lambda: tg.push_text(OWNER, "/add_reminder через час купить хлеб"))
    assert script.seen[-1].user_prompts[-1] == "Создай напоминание: через час купить хлеб"
    assert texts_of(await send(lambda: tg.push_text(OWNER, "/add_reminder"))) == [texts.ADD_REMINDER_PROMPT]
    script.replies = ["Во сколько?"]
    await send(lambda: tg.push_text(OWNER, "в пятницу про встречу"))
    assert script.seen[-1].user_prompts[-1] == "Создай напоминание: в пятницу про встречу"


async def test_reminder_is_really_sent_with_buttons_and_snooze_works(rbot, assistant, sessions) -> None:
    send, tg, bot = rbot
    user = await assistant.get_user(OWNER)
    service = ReminderService(sessions, now=lambda: CLOCK.now)
    r = await service.create(user, "позвонить врачу", in_minutes=5)
    delivery = ReminderDelivery(service=service, sessions=sessions, sender=TelegramSender(bot),
                                on_sent=assistant.record_outgoing)
    CLOCK.advance(minutes=5)
    assert await delivery.tick() == 1
    method, params = next((m, p) for m, p in tg.calls if m == "sendMessage")
    assert int(params["chat_id"]) == OWNER and params["text"] == "⏰ Напоминание: позвонить врачу"
    assert {f"rem:done:{r.id}", f"rem:snz:{r.id}:15", f"rem:snz:{r.id}:60", f"rem:snz:{r.id}:1440"} <= \
        buttons([(method, params)])
    out = texts_of(await send(lambda: tg.push_callback(OWNER, f"rem:snz:{r.id}:15")))
    assert out == [texts.REMINDER_SNOOZED.format(when="сегодня в 14:20")]
    CLOCK.advance(minutes=15)
    assert await delivery.tick() == 1  # отложенное пришло снова
    await send(lambda: tg.push_callback(OWNER, f"rem:done:{r.id}"))
    assert await service.list_active(user.id) == []


async def test_blocked_bot_is_permanent_failure(rbot, assistant, sessions) -> None:
    send, tg, bot = rbot
    user = await assistant.get_user(OWNER)
    service = ReminderService(sessions, now=lambda: CLOCK.now)
    await service.create(user, "x", in_minutes=1)
    tg.blocked_chats.add(OWNER)
    delivery = ReminderDelivery(service=service, sessions=sessions, sender=TelegramSender(bot))
    CLOCK.advance(minutes=1)
    assert await delivery.tick() == 0
    CLOCK.advance(minutes=10)
    assert await delivery.tick() == 0 and await service.list_active(user.id) == []  # не долбим заблокированный чат


async def test_proactive_toggle_timezone_and_settings(rbot, assistant) -> None:
    send, tg, _ = rbot
    out = texts_of(await send(lambda: tg.push_text(OWNER, "/proactive_on")))[0]
    assert "включены" in out and "22:00–09:00" in out
    assert (await assistant.get_user(OWNER)).proactive_enabled
    assert texts_of(await send(lambda: tg.push_text(OWNER, "/proactive_off"))) == [texts.PROACTIVE_OFF]
    out = texts_of(await send(lambda: tg.push_text(OWNER, "/timezone Europe/Warsaw")))[0]
    assert "Europe/Warsaw" in out and (await assistant.get_user(OWNER)).timezone == "Europe/Warsaw"
    assert texts_of(await send(lambda: tg.push_text(OWNER, "/timezone Mars/Olympus"))) == [texts.TIMEZONE_BAD]

    calls = await send(lambda: tg.push_text(OWNER, texts.BTN_SETTINGS))
    assert "Часовой пояс: Europe/Warsaw" in texts_of(calls)[0]
    assert {"pro:toggle", "pro:freq:rare", "pro:quiet:23-8", "pro:tz:kyiv"} <= buttons(calls)
    for data in ("pro:toggle", "pro:freq:often", "pro:quiet:23-8", "pro:tz:kyiv", "pro:freq:spam", "pro:tz:mars"):
        await send(lambda data=data: tg.push_callback(OWNER, data))
    user = await assistant.get_user(OWNER)
    assert (user.proactive_enabled, user.proactive_frequency, user.quiet_start, user.quiet_end, user.timezone) == \
        (True, "often", 23 * 60, 8 * 60, "Europe/Kyiv")  # подделанные значения отброшены


async def test_edit_reminder_via_button(rbot, assistant, script) -> None:
    send, tg, _ = rbot
    user = await assistant.get_user(OWNER)
    r = await assistant.reminders.create(user, "встреча", in_minutes=60)
    assert texts_of(await send(lambda: tg.push_callback(OWNER, f"rem:edit:{r.id}"))) == \
        [texts.EDIT_REMINDER_PROMPT.format(id=r.id)]
    script.replies = [{"tool": "update_reminder", "args": {"reminder_id": r.id, "in_minutes": 120}}, "Перенёс."]
    out = texts_of(await send(lambda: tg.push_text(OWNER, "на час позже")))
    assert script.seen[0].user_prompts[-1] == f"Измени напоминание #{r.id}: на час позже"
    assert out[0] == "Перенёс." and out[1].startswith(f"✏️ Изменено напоминание #{r.id}")
