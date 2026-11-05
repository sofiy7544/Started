"""Главное меню, команды, выбор модели, настройки, статистика."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from pai.bot import texts
from pai.bot.app import AppContext
from pai.services import stats as stats_service
from pai.services.assistant import REPLY_STYLES


class EditInstructions(StatesGroup):
    waiting = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_NEW), KeyboardButton(text=texts.BTN_MEMORY)],
            [KeyboardButton(text=texts.BTN_MODEL), KeyboardButton(text=texts.BTN_VOICE)],
            [KeyboardButton(text=texts.BTN_REMINDERS), KeyboardButton(text=texts.BTN_SETTINGS)],
            [KeyboardButton(text=texts.BTN_STATS), KeyboardButton(text=texts.BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите сообщение…",
    )


def _price_hint(model) -> str:  # type: ignore[no-untyped-def]
    cost = model.cost_usd(input_tokens=1_000_000, output_tokens=0)
    out = model.cost_usd(input_tokens=0, output_tokens=1_000_000)
    if cost == 0 and out == 0:
        return "бесплатно"
    return f"${cost:g} / ${out:g}"


def models_keyboard(app: AppContext, current_id: str) -> InlineKeyboardMarkup:
    rows = []
    for m in app.assistant.catalog.available(app.settings):
        mark = "✅ " if m.id == current_id else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{m.title} · {_price_hint(m)}", callback_data=f"model:{m.id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_text(app: AppContext) -> str:
    lines = [texts.MODEL_HEADER, ""]
    for m in app.assistant.catalog.available(app.settings):
        lines.append(f"• {m.title} — {m.note}")
    return "\n".join(lines)


def settings_keyboard(style: str, user=None) -> InlineKeyboardMarkup:  # type: ignore[no-untyped-def]
    style_row = [
        InlineKeyboardButton(text=("✅ " if key == style else "") + texts.STYLE_TITLES[key],
                             callback_data=f"style:{key}")
        for key in REPLY_STYLES
    ]
    rows = [
        style_row,
        [InlineKeyboardButton(text="✏️ Мои инструкции", callback_data="instr:edit")],
        [InlineKeyboardButton(text="🗑 Удалить инструкции", callback_data="instr:clear")],
    ]
    if user is not None:
        from pai.bot.handlers.reminders import schedule_settings_rows

        rows += [[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                 for row in schedule_settings_rows(user)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _user(app: AppContext, message_or_cb):  # type: ignore[no-untyped-def]
    u = message_or_cb.from_user
    return await app.assistant.get_user(u.id, u.first_name)


async def cmd_start(message: Message, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await _user(app, message)
    await message.answer(texts.welcome(user.first_name), reply_markup=main_keyboard())


async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню внизу экрана 👇", reply_markup=main_keyboard())


async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=main_keyboard())


async def cmd_new(message: Message, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    await app.assistant.new_conversation(await _user(app, message))
    await message.answer(texts.NEW_CONVERSATION)


async def cmd_model(message: Message, app: AppContext) -> None:
    user = await _user(app, message)
    current = app.assistant.current_model(user)
    await message.answer(models_text(app), reply_markup=models_keyboard(app, current.id))


async def cb_model(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    model = await app.assistant.set_model(user, (callback.data or "").split(":", 1)[1])
    if model is None:
        await callback.answer("Эта модель сейчас недоступна", show_alert=True)
        return
    await callback.answer(model.title)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=models_keyboard(app, model.id))
        await callback.message.answer(texts.model_set(model.title))


async def cmd_stats(message: Message, app: AppContext) -> None:
    user = await _user(app, message)
    st = await stats_service.collect(app.sessions, user.id)
    await message.answer(texts.stats_text(
        st, budget=app.assistant.budget.limit, days_left=stats_service.days_left_in_month()
    ))


async def _settings_message(app: AppContext, user) -> str:  # type: ignore[no-untyped-def]
    from pai.proactive.service import FREQUENCY_TITLES

    return texts.settings_text(
        model_title=app.assistant.current_model(user).title, style=user.reply_style,
        instructions=user.instructions, budget=app.assistant.budget.limit,
        spent=await app.assistant.budget.spent_this_month(),
    ) + (
        f"\n\n🔔 Инициативные сообщения: {'вкл' if user.proactive_enabled else 'выкл'}, "
        f"{FREQUENCY_TITLES.get(user.proactive_frequency, user.proactive_frequency)}\n"
        f"🌙 Тихие часы: {texts.quiet_text(user.quiet_start, user.quiet_end)}\n"
        f"🌍 Часовой пояс: {user.timezone} (другой — /timezone Europe/…)"
    )


async def settings_message_and_keyboard(app: AppContext, user):  # type: ignore[no-untyped-def]
    return await _settings_message(app, user), settings_keyboard(user.reply_style, user)


async def cmd_settings(message: Message, app: AppContext) -> None:
    user = await _user(app, message)
    text, kb = await settings_message_and_keyboard(app, user)
    await message.answer(text, reply_markup=kb)


async def cb_style(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    style = (callback.data or "").split(":", 1)[1]
    if not await app.assistant.set_reply_style(user, style):
        await callback.answer()
        return
    await callback.answer(f"Стиль: {texts.STYLE_TITLES[style]}")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(await _settings_message(app, user),
                                         reply_markup=settings_keyboard(style, user))


async def cb_instructions_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditInstructions.waiting)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.INSTRUCTIONS_PROMPT)


async def cb_instructions_clear(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    await app.assistant.set_instructions(user, None)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(await _settings_message(app, user),
                                         reply_markup=settings_keyboard(user.reply_style, user))
        await callback.message.answer(texts.INSTRUCTIONS_CLEARED)


async def instructions_received(message: Message, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    await app.assistant.set_instructions(await _user(app, message), message.text or "")
    await message.answer(texts.INSTRUCTIONS_SAVED, reply_markup=main_keyboard())


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=main_keyboard())


def create_router() -> Router:
    r = Router(name="menu")
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.message.register(cmd_cancel, Command("cancel"))
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_menu, Command("menu"))
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_help, F.text == texts.BTN_HELP)
    r.message.register(cmd_new, Command("new"))
    r.message.register(cmd_new, F.text == texts.BTN_NEW)
    r.message.register(cmd_model, Command("model"))
    r.message.register(cmd_model, F.text == texts.BTN_MODEL)
    r.message.register(cmd_stats, Command("stats"))
    r.message.register(cmd_stats, F.text == texts.BTN_STATS)
    r.message.register(cmd_settings, Command("settings"))
    r.message.register(cmd_settings, F.text == texts.BTN_SETTINGS)
    r.message.register(instructions_received, StateFilter(EditInstructions.waiting), F.text)
    r.callback_query.register(cb_model, F.data.startswith("model:"))
    r.callback_query.register(cb_style, F.data.startswith("style:"))
    r.callback_query.register(cb_instructions_edit, F.data == "instr:edit")
    r.callback_query.register(cb_instructions_clear, F.data == "instr:clear")
    return r
