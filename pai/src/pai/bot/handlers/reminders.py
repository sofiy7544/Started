"""Напоминания и инициатива в Telegram: команды, кнопки, карточки подтверждения, часовой пояс."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from pai.bot import texts
from pai.bot.app import AppContext, OwnerOnly
from pai.proactive.service import FREQUENCY_TITLES
from pai.reminders.schedule import ScheduleError, zone
from pai.reminders.service import ReminderError

QUIET_PRESETS = {
    "22-9": (22 * 60, 9 * 60),
    "23-8": (23 * 60, 8 * 60),
    "21-10": (21 * 60, 10 * 60),
    "off": (0, 0),
}
TIMEZONES = {
    "kyiv": "Europe/Kyiv",
    "warsaw": "Europe/Warsaw",
    "berlin": "Europe/Berlin",
    "london": "Europe/London",
    "utc": "UTC",
}


class ReminderStates(StatesGroup):
    adding = State()
    editing = State()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


async def _user(app: AppContext, event):  # type: ignore[no-untyped-def]
    return await app.assistant.get_user(event.from_user.id, event.from_user.first_name)


async def send_cards(message: Message, answer) -> None:  # type: ignore[no-untyped-def]
    """Подтверждения по факту сделанного инструментами — не по словам модели."""
    effects = getattr(answer, "reminders", None)
    if effects is None:
        return
    for kind, views in (
        ("created", effects.created),
        ("updated", effects.updated),
        ("deleted", effects.deleted),
    ):
        for v in views:
            kb = (
                None
                if kind == "deleted"
                else _kb([[("✏️ Изменить", f"rem:edit:{v.id}"), ("❌ Удалить", f"rem:del:{v.id}")]])
            )
            await message.answer(texts.reminder_card(kind, v), reply_markup=kb)


async def _ask_and_reply(message: Message, bot: Bot, app: AppContext, prompt: str) -> None:
    from pai.bot.handlers.voice import send_answer

    user = await _user(app, message)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        answer = await app.assistant.ask(user, prompt)
    await send_answer(message, bot, app, user, answer, incoming_voice=False)


# ---- список, добавление, удаление --------------------------------------------------------


async def cmd_reminders(message: Message, app: AppContext) -> None:
    if app.assistant.reminders is None:
        return
    user = await _user(app, message)
    items = await app.assistant.reminders.list_active(user.id)
    if not items:
        await message.answer(texts.REMINDERS_EMPTY)
        return
    lines = [texts.REMINDERS_HEADER, ""] + [v.line() for v in items[:30]]
    rows = [[(f"✏️ #{v.id}", f"rem:edit:{v.id}"), (f"❌ #{v.id}", f"rem:del:{v.id}")] for v in items[:30]]
    await message.answer("\n".join(lines), reply_markup=_kb(rows))


async def cmd_add(
    message: Message, command: CommandObject, bot: Bot, app: AppContext, state: FSMContext
) -> None:
    if command.args:
        await _ask_and_reply(message, bot, app, f"Создай напоминание: {command.args}")
        return
    await state.set_state(ReminderStates.adding)
    await message.answer(texts.ADD_REMINDER_PROMPT)


async def add_received(message: Message, bot: Bot, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    await _ask_and_reply(message, bot, app, f"Создай напоминание: {message.text}")


async def cmd_delete(message: Message, command: CommandObject, app: AppContext) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await cmd_reminders(message, app)
        return
    user = await _user(app, message)
    try:
        await app.assistant.reminders.cancel(user.id, int(arg))
    except ReminderError:
        await message.answer(texts.REMINDER_NOT_FOUND)
        return
    await message.answer(texts.REMINDER_DELETED.format(id=arg))


async def cb_delete(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    rid = int((callback.data or "").rsplit(":", 1)[1])
    try:
        await app.assistant.reminders.cancel(user.id, rid)  # только своё: запрос ограничен user_id
    except ReminderError:
        await callback.answer(texts.REMINDER_NOT_FOUND)
        return
    await callback.answer("Удалено")
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.REMINDER_DELETED.format(id=rid))


async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    rid = int((callback.data or "").rsplit(":", 1)[1])
    await state.set_state(ReminderStates.editing)
    await state.update_data(reminder_id=rid)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.EDIT_REMINDER_PROMPT.format(id=rid))


async def edit_received(message: Message, bot: Bot, app: AppContext, state: FSMContext) -> None:
    rid = (await state.get_data()).get("reminder_id")
    await state.clear()
    await _ask_and_reply(message, bot, app, f"Измени напоминание #{rid}: {message.text}")


async def cb_done(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    await app.assistant.reminders.mark_done(user.id, int((callback.data or "").rsplit(":", 1)[1]))
    await callback.answer(texts.REMINDER_DONE)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


async def cb_snooze(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    _, _, rid, minutes = (callback.data or "").split(":")
    if minutes not in ("15", "60", "1440"):
        await callback.answer()
        return
    try:
        view = await app.assistant.reminders.snooze(user.id, int(rid), int(minutes))
    except ReminderError:
        await callback.answer(texts.REMINDER_NOT_FOUND)
        return
    await callback.answer("Отложено")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(texts.REMINDER_SNOOZED.format(when=view.when))


# ---- инициатива и часовой пояс -----------------------------------------------------------


async def cmd_proactive_on(message: Message, app: AppContext) -> None:
    user = await _user(app, message)
    await app.assistant.set_schedule_prefs(user, proactive_enabled=True)
    await message.answer(
        texts.PROACTIVE_ON.format(
            freq=FREQUENCY_TITLES[user.proactive_frequency],
            quiet=texts.quiet_text(user.quiet_start, user.quiet_end),
        )
    )


async def cmd_proactive_off(message: Message, app: AppContext) -> None:
    await app.assistant.set_schedule_prefs(await _user(app, message), proactive_enabled=False)
    await message.answer(texts.PROACTIVE_OFF)


async def cmd_timezone(message: Message, command: CommandObject, app: AppContext) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer(texts.TIMEZONE_USAGE)
        return
    try:
        tz = zone(name)
    except ScheduleError:
        await message.answer(texts.TIMEZONE_BAD)
        return
    await app.assistant.set_schedule_prefs(await _user(app, message), timezone=name)
    await message.answer(texts.TIMEZONE_SET.format(tz=name, now=f"{datetime.now(UTC).astimezone(tz):%H:%M}"))


def schedule_settings_rows(user) -> list[list[tuple[str, str]]]:  # type: ignore[no-untyped-def]
    mark = lambda ok: "✅ " if ok else ""  # noqa: E731
    quiet = (user.quiet_start, user.quiet_end)
    return [
        [("🔔 Инициатива: " + ("вкл" if user.proactive_enabled else "выкл"), "pro:toggle")],
        [
            (mark(user.proactive_frequency == k) + t.split(" (")[0], f"pro:freq:{k}")
            for k, t in FREQUENCY_TITLES.items()
        ],
        [
            (
                mark(quiet == v) + ("без тихих" if k == "off" else "🌙 " + k.replace("-", "–")),
                f"pro:quiet:{k}",
            )
            for k, v in QUIET_PRESETS.items()
        ],
        [
            (
                mark(user.timezone == v) + k.capitalize() if k != "utc" else mark(user.timezone == v) + "UTC",
                f"pro:tz:{k}",
            )
            for k, v in TIMEZONES.items()
        ],
    ]


async def cb_schedule(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    parts = (callback.data or "").split(":")
    if parts[1] == "toggle":
        await app.assistant.set_schedule_prefs(user, proactive_enabled=not user.proactive_enabled)
    elif parts[1] == "freq" and parts[2] in FREQUENCY_TITLES:
        await app.assistant.set_schedule_prefs(user, proactive_frequency=parts[2])
    elif parts[1] == "quiet" and parts[2] in QUIET_PRESETS:
        start, end = QUIET_PRESETS[parts[2]]
        await app.assistant.set_schedule_prefs(user, quiet_start=start, quiet_end=end)
    elif parts[1] == "tz" and parts[2] in TIMEZONES:
        await app.assistant.set_schedule_prefs(user, timezone=TIMEZONES[parts[2]])
    else:
        await callback.answer()
        return
    await callback.answer("Сохранено")
    if isinstance(callback.message, Message):
        from pai.bot.handlers.menu import settings_message_and_keyboard

        text, kb = await settings_message_and_keyboard(app, user)
        await callback.message.edit_text(text, reply_markup=kb)


def create_router() -> Router:
    r = Router(name="reminders")
    r.message.filter(F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.callback_query.filter(OwnerOnly())
    r.message.register(cmd_reminders, Command("reminders"))
    r.message.register(cmd_reminders, F.text == texts.BTN_REMINDERS)
    r.message.register(cmd_add, Command("add_reminder"))
    r.message.register(cmd_delete, Command("delete_reminder"))
    r.message.register(cmd_proactive_on, Command("proactive_on"))
    r.message.register(cmd_proactive_off, Command("proactive_off"))
    r.message.register(cmd_timezone, Command("timezone"))
    r.message.register(add_received, StateFilter(ReminderStates.adding), F.text)
    r.message.register(edit_received, StateFilter(ReminderStates.editing), F.text)
    r.callback_query.register(cb_delete, F.data.startswith("rem:del:"))
    r.callback_query.register(cb_edit, F.data.startswith("rem:edit:"))
    r.callback_query.register(cb_done, F.data.startswith("rem:done:"))
    r.callback_query.register(cb_snooze, F.data.startswith("rem:snz:"))
    r.callback_query.register(cb_schedule, F.data.startswith("pro:"))
    return r
