"""Интерфейс памяти: обзор, записи (правка/удаление), поиск, экспорт, очистка, полное удаление."""

from __future__ import annotations

import json
from datetime import UTC

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from pai.bot import texts
from pai.bot.app import AppContext, OwnerOnly, split_message
from pai.memory.store import KIND_TITLES

PAGE_SIZE = 8


class MemoryStates(StatesGroup):
    editing = State()
    searching = State()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def overview_keyboard() -> InlineKeyboardMarkup:
    return _kb([
        [("📋 Записи", "mem:list:0"), ("🔍 Поиск", "mem:search")],
        [("📤 Экспорт", "mem:export"), ("🗑 Очистить память", "mem:clear")],
        [("⚠️ Удалить все мои данные", "mem:wipe")],
    ])


def _date(dt) -> str:  # type: ignore[no-untyped-def]
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).strftime("%d.%m.%Y")


async def _user(app: AppContext, event):  # type: ignore[no-untyped-def]
    return await app.assistant.get_user(event.from_user.id, event.from_user.first_name)


async def overview_text(app: AppContext, user_id: int) -> str:
    store = app.assistant.memory.store
    counts = await store.counts(user_id)
    episodes = await store.recent_episodes(user_id, 3)
    total = sum(counts.values())
    lines = ["🧠 Память", ""]
    if not total and not episodes:
        lines.append(texts.MEMORY_EMPTY)
    else:
        lines.append(f"Сохранено записей: {total}")
        lines += [f"• {KIND_TITLES[k]}: {n}" for k, n in counts.items() if n]
        if episodes:
            lines += ["", "Последние обсуждения:"]
            lines += [f"• {_date(e.created_at)} — {e.summary}" for e in episodes]
        lines.append(f"\nВсего эпизодов: {await store.episodes_count(user_id)}")
    lines += ["", texts.MEMORY_HINT]
    return "\n".join(lines)


async def cmd_memory(message: Message, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    if app.assistant.memory is None:
        await message.answer(texts.MEMORY_DISABLED)
        return
    user = await _user(app, message)
    await message.answer(await overview_text(app, user.id), reply_markup=overview_keyboard())


async def cmd_remember(message: Message, command: CommandObject, app: AppContext) -> None:
    if not command.args:
        await message.answer(texts.REMEMBER_USAGE)
        return
    saved = await app.assistant.remember(await _user(app, message), command.args)
    await message.answer(texts.REMEMBERED if saved else texts.REMEMBER_REJECTED)


async def cb_list(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    page = int((callback.data or "mem:list:0").rsplit(":", 1)[1])
    items = await app.assistant.memory.store.active(user.id)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if not items:
        await callback.message.answer(texts.MEMORY_EMPTY)
        return
    pages = (len(items) - 1) // PAGE_SIZE + 1
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = [f"📋 Записи памяти (стр. {page + 1}/{pages})", ""]
    for m in chunk:
        sure = "" if m.confidence >= 0.7 else f", уверенность {int(m.confidence * 100)}%"
        who = ", от вас" if m.source == "user" else ""
        lines.append(f"#{m.id} [{KIND_TITLES[m.kind]}] {m.content} ({_date(m.updated_at)}{sure}{who})")
    rows = [[(f"✏️ #{m.id}", f"mem:edit:{m.id}"), (f"❌ #{m.id}", f"mem:del:{m.id}")] for m in chunk]
    nav = []
    if page > 0:
        nav.append(("◀️", f"mem:list:{page - 1}"))
    if page < pages - 1:
        nav.append(("▶️", f"mem:list:{page + 1}"))
    if nav:
        rows.append(nav)
    await callback.message.answer("\n".join(lines), reply_markup=_kb(rows))


async def cb_delete(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    memory_id = int((callback.data or "").rsplit(":", 1)[1])
    # Удаляется только своя запись: хранилище ограничивает запрос user_id.
    ok = await app.assistant.memory.store.delete(user.id, memory_id)
    if ok:
        await app.assistant.audit(user.tg_id, "memory_deleted_by_user")
    await callback.answer(texts.MEMORY_DELETED if ok else texts.MEMORY_NOT_FOUND)


async def cb_edit(callback: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    user = await _user(app, callback)
    memory_id = int((callback.data or "").rsplit(":", 1)[1])
    memory = await app.assistant.memory.store.get(user.id, memory_id)
    await callback.answer()
    if memory is None or not isinstance(callback.message, Message):
        return
    await state.set_state(MemoryStates.editing)
    await state.update_data(memory_id=memory_id)
    await callback.message.answer(texts.memory_edit_prompt(memory.content))


async def edit_received(message: Message, app: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    user = await _user(app, message)
    edited = await app.assistant.memory.store.edit(user.id, int(data.get("memory_id", 0)), message.text or "")
    if edited:
        await app.assistant.audit(user.tg_id, "memory_edited_by_user")
    await message.answer(texts.MEMORY_EDITED if edited else texts.REMEMBER_REJECTED)


async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MemoryStates.searching)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.MEMORY_SEARCH_PROMPT)


async def search_received(message: Message, app: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await _user(app, message)
    found = await app.assistant.memory.store.search(user.id, message.text or "", memories=10, episodes=5)
    if not found.memories and not found.episodes:
        await message.answer(texts.MEMORY_SEARCH_NOTHING)
        return
    lines = ["🔍 Найдено", ""]
    lines += [f"#{m.id} [{KIND_TITLES[m.kind]}] {m.content}" for m in found.memories]
    if found.episodes:
        lines += ["", "Обсуждения:"]
        for e in found.episodes:
            extra = f" Решения: {'; '.join(e.decisions.splitlines())}." if e.decisions else ""
            lines.append(f"• {_date(e.created_at)} — {e.summary}{extra}")
    for part in split_message("\n".join(lines)):
        await message.answer(part)


async def cb_export(callback: CallbackQuery, app: AppContext) -> None:
    user = await _user(app, callback)
    data = await app.assistant.memory.store.export(user.id)
    await app.assistant.audit(user.tg_id, "memory_exported")
    await callback.answer()
    if isinstance(callback.message, Message):
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        await callback.message.answer_document(
            BufferedInputFile(payload, filename="memory-export.json"), caption=texts.MEMORY_EXPORTED
        )


async def cb_clear(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.MEMORY_CLEAR_CONFIRM, reply_markup=_kb(
            [[("Да, очистить память", "mem:clear:yes"), ("Отмена", "mem:cancel")]]))


async def cb_clear_yes(callback: CallbackQuery, app: AppContext) -> None:
    await app.assistant.clear_memory(await _user(app, callback))
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.MEMORY_CLEARED)


async def cb_wipe(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.WIPE_CONFIRM, reply_markup=_kb(
            [[("Да, удалить всё", "mem:wipe:yes"), ("Отмена", "mem:cancel")]]))


async def cb_wipe_yes(callback: CallbackQuery, app: AppContext) -> None:
    await app.assistant.wipe_user(await _user(app, callback))
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.WIPED)


async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.CANCELLED)


def create_router() -> Router:
    r = Router(name="memory")
    r.message.filter(F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.callback_query.filter(OwnerOnly())
    r.message.register(cmd_memory, Command("memory"))
    r.message.register(cmd_memory, F.text == texts.BTN_MEMORY)
    r.message.register(cmd_remember, Command("remember"))
    r.message.register(edit_received, StateFilter(MemoryStates.editing), F.text)
    r.message.register(search_received, StateFilter(MemoryStates.searching), F.text)
    r.callback_query.register(cb_list, F.data.startswith("mem:list:"))
    r.callback_query.register(cb_delete, F.data.startswith("mem:del:"))
    r.callback_query.register(cb_edit, F.data.startswith("mem:edit:"))
    r.callback_query.register(cb_search, F.data == "mem:search")
    r.callback_query.register(cb_export, F.data == "mem:export")
    r.callback_query.register(cb_clear, F.data == "mem:clear")
    r.callback_query.register(cb_clear_yes, F.data == "mem:clear:yes")
    r.callback_query.register(cb_wipe, F.data == "mem:wipe")
    r.callback_query.register(cb_wipe_yes, F.data == "mem:wipe:yes")
    r.callback_query.register(cb_cancel, F.data == "mem:cancel")
    return r
