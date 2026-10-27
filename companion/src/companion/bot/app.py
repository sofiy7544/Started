"""Сборка aiogram Dispatcher: роутеры, защита от повторных апдейтов, обработка ошибок."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import BotCommand, ErrorEvent, TelegramObject, Update

from companion import texts
from companion.config import Settings
from companion.persona import Persona
from companion.repositories import users
from companion.services.chat import ChatService

log = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="reset", description="Начать разговор заново"),
    BotCommand(command="memory", description="Что она обо мне помнит"),
    BotCommand(command="forget", description="Стереть память или данные"),
    BotCommand(command="privacy", description="Какие данные хранятся"),
]


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    persona: Persona
    chat: ChatService


class DedupMiddleware(BaseMiddleware):
    """Каждый update_id обрабатывается один раз, даже если Telegram прислал его повторно
    (например, после перезапуска до подтверждения offset)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and not await users.mark_update_processed(
            self._pool, event.update_id
        ):
            log.info("duplicate update skipped: %s", event.update_id)
            return None
        return await handler(event, data)


async def on_error(event: ErrorEvent) -> bool:
    # Только тип и id: текст переписки в логи не пишем.
    log.error(
        "unhandled error in update %s: %s", event.update.update_id, type(event.exception).__name__,
        exc_info=event.exception,
    )
    message = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    if message is not None and hasattr(message, "answer"):
        try:
            await message.answer(texts.INTERNAL_ERROR)
        except Exception:  # noqa: BLE001 — сообщить об ошибке не удалось, уже залогировано
            pass
    return True


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст по абзацам, строкам, предложениям или словам — не посреди слова."""
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


def build_dispatcher(app: AppContext, pool: asyncpg.Pool) -> Dispatcher:
    from companion.bot.handlers import chat, commands

    dp = Dispatcher(app=app)
    dp.update.outer_middleware(DedupMiddleware(pool))
    dp.errors.register(on_error)
    dp.include_router(commands.create_router())
    dp.include_router(chat.create_router())
    return dp


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(COMMANDS)
