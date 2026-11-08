"""Список задач в Telegram: /tasks, кнопка «📋 Задачи», выгрузка в Excel."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from pai.bot import texts
from pai.bot.app import AppContext, split_message
from pai.media.outputs import build_file
from pai.tasks.service import grouped_text, to_csv_table

KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Выгрузить в Excel", callback_data="tasks:xlsx")]])


async def cmd_tasks(message: Message, app: AppContext) -> None:
    if app.assistant.tasks is None:
        return
    u = message.from_user
    user = await app.assistant.get_user(u.id, u.first_name)
    items = await app.assistant.tasks.list(user.id)
    closed = len(await app.assistant.tasks.list(user.id, include_closed=True)) - len(items)
    if not items:
        await message.answer(texts.TASKS_EMPTY, reply_markup=KB if closed else None)
        return
    body = texts.TASKS_HEADER.format(n=len(items)) + "\n" + grouped_text(items)
    if closed:
        body += "\n\n" + texts.TASKS_ALL_DONE.format(n=closed)
    parts = split_message(body)
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=KB if i == len(parts) - 1 else None)


async def cb_xlsx(callback: CallbackQuery, app: AppContext) -> None:
    await callback.answer()
    if app.assistant.tasks is None or not isinstance(callback.message, Message):
        return
    user = await app.assistant.get_user(callback.from_user.id, callback.from_user.first_name)
    items = await app.assistant.tasks.list(user.id, include_closed=True)
    f = build_file("Задачи.xlsx", to_csv_table(items))
    await callback.message.answer_document(BufferedInputFile(f.data, filename=f.filename))


def create_router() -> Router:
    r = Router(name="tasks")
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.message.register(cmd_tasks, Command("tasks"))
    r.message.register(cmd_tasks, F.text == texts.BTN_TASKS)
    r.callback_query.register(cb_xlsx, F.data == "tasks:xlsx")
    return r
