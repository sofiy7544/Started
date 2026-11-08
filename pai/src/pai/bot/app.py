"""Сборка Dispatcher: белый список, защита от повторов, обработка ошибок, роутеры."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, TelegramObject, Update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.bot import texts
from pai.config import Settings
from pai.db.models import AuditEvent, ProcessedUpdate
from pai.media.engine import MediaEngine
from pai.services.assistant import Assistant
from pai.voice.engine import VoiceEngine

log = logging.getLogger(__name__)
TELEGRAM_MESSAGE_LIMIT = 4096

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="new", description="Новый диалог"),
    BotCommand(command="memory", description="Память: что я о вас помню"),
    BotCommand(command="remember", description="Запомнить: /remember текст"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="voice", description="Голос: режим, тембр, скорость"),
    BotCommand(command="tasks", description="Список задач (план работ)"),
    BotCommand(command="reminders", description="Мои напоминания"),
    BotCommand(command="add_reminder", description="Создать напоминание"),
    BotCommand(command="delete_reminder", description="Удалить напоминание"),
    BotCommand(command="proactive_on", description="Включить инициативные сообщения"),
    BotCommand(command="proactive_off", description="Выключить инициативные сообщения"),
    BotCommand(command="stats", description="Статистика расходов"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
]


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    assistant: Assistant
    sessions: async_sessionmaker
    voice: VoiceEngine | None = None
    media: MediaEngine | None = None


def _sender_id(update: Update) -> int | None:
    for event in (update.message, update.edited_message, update.callback_query):
        if event is not None and event.from_user is not None:
            return event.from_user.id
    return None


class AccessMiddleware(BaseMiddleware):
    """Белый список Telegram ID. Посторонний не доходит ни до одного обработчика и не
    тратит деньги; отказ ему показываем не чаще раза в час, попытку пишем в журнал."""

    def __init__(self, allowed: set[int], sessions: async_sessionmaker,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._allowed = allowed
        self._sessions = sessions
        self._clock = clock
        self._last_notice: dict[int, float] = {}

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        sender = _sender_id(event)
        if sender in self._allowed:
            return await handler(event, data)
        if sender is None:
            return None  # служебные апдейты без отправителя не обрабатываем
        now = self._clock()
        if now - self._last_notice.get(sender, -1e9) >= 3600:
            self._last_notice[sender] = now
            log.warning("access denied for telegram id %s", sender)
            async with self._sessions() as s, s.begin():
                s.add(AuditEvent(tg_id=sender, kind="access_denied"))
            bot: Bot | None = data.get("bot")
            chat = event.message.chat.id if event.message else None
            if bot and chat is not None:
                try:
                    await bot.send_message(chat, texts.ACCESS_DENIED)
                except Exception:  # noqa: BLE001 — не мешаем работе из-за недоставленного отказа
                    pass
        if event.callback_query is not None:
            try:
                await event.callback_query.answer(texts.ACCESS_DENIED)
            except Exception:  # noqa: BLE001
                pass
        return None


class DedupMiddleware(BaseMiddleware):
    """Каждый update_id обрабатывается один раз (например, после перезапуска)."""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            try:
                async with self._sessions() as s, s.begin():
                    s.add(ProcessedUpdate(update_id=event.update_id))
            except IntegrityError:
                log.info("duplicate update skipped: %s", event.update_id)
                return None
        return await handler(event, data)


async def on_error(event: ErrorEvent) -> bool:
    log.error("unhandled error in update %s: %s", event.update.update_id,
              type(event.exception).__name__, exc_info=event.exception)
    message = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    if message is not None and hasattr(message, "answer"):
        try:
            await message.answer(texts.INTERNAL_ERROR)
        except Exception:  # noqa: BLE001
            pass
    return True


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст по абзацам, строкам, предложениям или словам."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
            idx = window.rfind(sep)
            if idx > limit // 2:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def build_dispatcher(app: AppContext) -> Dispatcher:
    from pai.bot.collector import Collector
    from pai.bot.handlers import chat, media, memory, menu, reminders, tasks, voice

    dp = Dispatcher(storage=MemoryStorage(), app=app)
    collector = None
    if app.media is not None:
        collector = Collector(
            lambda batch: media.run_batch(app, batch),
            media_wait=app.settings.media_wait_seconds,
            text_wait=app.settings.media_text_wait_seconds,
        )
    dp["collector"] = collector
    # Сначала белый список: посторонние апдейты даже не попадают в журнал дедупликации.
    dp.update.outer_middleware(AccessMiddleware(set(app.settings.allowed_telegram_ids), app.sessions))
    dp.update.outer_middleware(DedupMiddleware(app.sessions))
    dp.errors.register(on_error)
    dp.include_router(menu.create_router())
    dp.include_router(memory.create_router())
    if app.media is not None:
        dp.include_router(media.create_router())
    dp.include_router(voice.create_router())
    dp.include_router(reminders.create_router())
    dp.include_router(tasks.create_router())
    dp.include_router(chat.create_router())
    return dp


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(COMMANDS)
