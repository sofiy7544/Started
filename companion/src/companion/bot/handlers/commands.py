from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from companion import texts
from companion.bot.app import AppContext

AGE_OK = "age:ok"
FORGET_MEMORY = "forget:mem"
FORGET_ALL = "forget:all"
FORGET_CANCEL = "forget:cancel"
CONFIRM_SUFFIX = ":yes"


def age_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.AGE_BUTTON, callback_data=AGE_OK)]]
    )


def _kb(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in buttons]
    )


async def cmd_start(message: Message, app: AppContext) -> None:
    u = message.from_user
    user = await app.chat.get_or_create_user(u.id, u.first_name, u.language_code)
    if user.age_confirmed:
        await message.answer(texts.welcome_back(app.persona))
    else:
        await message.answer(texts.welcome(app.persona), reply_markup=age_keyboard())


async def cb_age_ok(callback: CallbackQuery, app: AppContext) -> None:
    u = callback.from_user
    user = await app.chat.get_or_create_user(u.id, u.first_name, u.language_code)
    first_time = not user.age_confirmed
    await app.chat.confirm_age(user)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        if first_time:
            await callback.message.answer(texts.after_confirm(app.persona))


async def cmd_help(message: Message, app: AppContext) -> None:
    await message.answer(texts.help_text(app.persona, app.settings.support_contact))


async def cmd_privacy(message: Message, app: AppContext) -> None:
    await message.answer(texts.privacy_text(app.persona, app.settings.message_retention_days))


async def cmd_reset(message: Message, app: AppContext) -> None:
    user = await app.chat.find_user(message.from_user.id)
    if user is None or not user.age_confirmed:
        await message.answer(texts.AGE_REQUIRED)
        return
    await app.chat.reset_context(user)
    await message.answer(texts.RESET_DONE)


async def cmd_memory(message: Message, app: AppContext) -> None:
    user = await app.chat.find_user(message.from_user.id)
    if user is None:
        await message.answer(texts.memory_text(app.persona, [], 0, 0))
        return
    snap = await app.chat.memory_snapshot(user)
    await message.answer(
        texts.memory_text(app.persona, snap.facts, snap.summaries_count, snap.messages_count)
    )


async def cmd_forget(message: Message) -> None:
    await message.answer(
        texts.FORGET_MENU,
        reply_markup=_kb(
            (texts.FORGET_MEMORY_BUTTON, FORGET_MEMORY),
            (texts.FORGET_ALL_BUTTON, FORGET_ALL),
            (texts.CANCEL_BUTTON, FORGET_CANCEL),
        ),
    )


async def cb_forget_choose(callback: CallbackQuery) -> None:
    question = texts.FORGET_MEMORY_CONFIRM if callback.data == FORGET_MEMORY else texts.FORGET_ALL_CONFIRM
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            question,
            reply_markup=_kb(
                (texts.CONFIRM_BUTTON, callback.data + CONFIRM_SUFFIX),
                (texts.CANCEL_BUTTON, FORGET_CANCEL),
            ),
        )


async def cb_forget_confirm(callback: CallbackQuery, app: AppContext) -> None:
    # Пользователь определяется только по from_user.id — удалить чужие данные нельзя.
    user = await app.chat.find_user(callback.from_user.id)
    if callback.data == FORGET_ALL + CONFIRM_SUFFIX:
        if user is not None:
            await app.chat.delete_user(user)
        done = texts.FORGET_ALL_DONE
    else:
        if user is not None:
            await app.chat.forget_memory(user)
        done = texts.FORGET_MEMORY_DONE
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(done)


async def cb_forget_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.CANCELLED)


def create_router() -> Router:
    router = Router(name="commands")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_privacy, Command("privacy"))
    router.message.register(cmd_reset, Command("reset"))
    router.message.register(cmd_memory, Command("memory"))
    router.message.register(cmd_forget, Command("forget"))
    router.callback_query.register(cb_age_ok, F.data == AGE_OK)
    router.callback_query.register(cb_forget_choose, F.data.in_({FORGET_MEMORY, FORGET_ALL}))
    router.callback_query.register(
        cb_forget_confirm,
        F.data.in_({FORGET_MEMORY + CONFIRM_SUFFIX, FORGET_ALL + CONFIRM_SUFFIX}),
    )
    router.callback_query.register(cb_forget_cancel, F.data == FORGET_CANCEL)
    return router
