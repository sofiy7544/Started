"""Обычная переписка с ассистентом."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from pai.bot import texts
from pai.bot.app import AppContext, split_message


async def on_text(message: Message, bot: Bot, app: AppContext) -> None:
    u = message.from_user
    user = await app.assistant.get_user(u.id, u.first_name)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        answer = await app.assistant.ask(user, message.text or "")
    if answer.text:
        for part in split_message(answer.text):
            await message.answer(part)


async def unknown_command(message: Message) -> None:
    await message.answer(texts.UNKNOWN_COMMAND)


async def on_voice(message: Message) -> None:
    await message.answer(texts.NOT_YET_VOICE)


async def on_file(message: Message) -> None:
    await message.answer(texts.NOT_YET_FILES)


async def on_other(message: Message) -> None:
    await message.answer(texts.NOT_YET_OTHER)


def create_router() -> Router:
    r = Router(name="chat")
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.message.register(unknown_command, F.text.startswith("/"))
    r.message.register(on_text, F.text)
    r.message.register(on_voice, F.voice | F.audio | F.video_note)
    r.message.register(on_file, F.document | F.photo)
    r.message.register(on_other)
    return r
