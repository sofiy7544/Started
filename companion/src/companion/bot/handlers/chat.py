from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from companion import texts
from companion.bot.app import AppContext, split_message
from companion.bot.handlers.commands import age_keyboard


async def unknown_command(message: Message) -> None:
    await message.answer("Такой команды нет. Список — /help.")


async def on_text(message: Message, bot: Bot, app: AppContext) -> None:
    u = message.from_user
    user = await app.chat.get_or_create_user(u.id, u.first_name, u.language_code)
    if not user.age_confirmed:
        await message.answer(texts.AGE_REQUIRED, reply_markup=age_keyboard())
        return
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        reply = await app.chat.handle_message(user, message.text)
    if reply.text:
        for part in split_message(reply.text):
            await message.answer(part)


async def on_other(message: Message) -> None:
    await message.answer(texts.ONLY_TEXT)


def create_router() -> Router:
    router = Router(name="chat")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(unknown_command, F.text.startswith("/"))
    router.message.register(on_text, F.text)
    router.message.register(on_other)
    return router
