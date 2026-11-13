"""Обычная переписка с ассистентом."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from pai.bot import texts
from pai.bot.app import AppContext, OwnerOnly
from pai.bot.collector import Collector, Piece


async def on_text(message: Message, bot: Bot, app: AppContext, collector: Collector | None = None) -> None:
    from pai.bot.handlers.voice import send_answer

    u = message.from_user
    if collector is not None and collector.pending(u.id):
        # Текст сразу после файлов — задача к ним: «сравни», «что здесь не так».
        collector.add(u.id, bot, message, Piece(text=message.text or ""), instruction=True)
        return
    user = await app.assistant.get_user(u.id, u.first_name)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        answer = await app.assistant.ask(user, message.text or "")
    await send_answer(message, bot, app, user, answer, incoming_voice=False)


async def unknown_command(message: Message) -> None:
    await message.answer(texts.UNKNOWN_COMMAND)


async def on_file(message: Message) -> None:
    await message.answer(texts.NOT_YET_FILES)


async def on_other(message: Message) -> None:
    await message.answer(texts.NOT_YET_OTHER)


def create_router() -> Router:
    r = Router(name="chat")
    r.message.filter(F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.callback_query.filter(OwnerOnly())
    r.message.register(unknown_command, F.text.startswith("/"))
    r.message.register(on_text, F.text)
    r.message.register(on_file, F.document | F.photo)
    r.message.register(on_other)
    return r
