"""Сборщик сообщений: файлы, подписи, голосовые и текст, присланные подряд, — одна задача.

Telegram присылает альбом (несколько фото) отдельными сообщениями, а человек часто шлёт фото,
потом голосом или текстом объясняет, что сделать. Сборщик ждёт немного после последнего
сообщения (после файла без подписи — дольше, вдруг следом вопрос) и отдаёт всё вместе.
Обработка каждого файла (скачивание, разбор, распознавание речи) начинается сразу, параллельно
ожиданию — ожидание почти ничего не добавляет ко времени ответа.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from pai.media.store import AttachmentView

log = logging.getLogger(__name__)


@dataclass
class Piece:
    """Результат одного сообщения: текст владельца (подпись, текст, речь) и/или вложения."""

    text: str = ""
    attachments: list[AttachmentView] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


@dataclass
class Batch:
    bot: Bot
    message: Message  # последнее сообщение — на него отвечаем
    pieces: list[asyncio.Future[Piece]] = field(default_factory=list)
    has_instruction: bool = False
    voice: bool = False  # было голосовое — можно ответить голосом
    files: int = 0
    timer: asyncio.TimerHandle | None = None


class Collector:
    def __init__(
        self,
        run: Callable[[Batch], Awaitable[None]],
        *,
        media_wait: float,
        text_wait: float,
    ) -> None:
        self._run = run
        self._media_wait = media_wait
        self._text_wait = text_wait
        self._batches: dict[int, Batch] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def pending(self, key: int) -> bool:
        return key in self._batches

    def files_in_batch(self, key: int) -> int:
        b = self._batches.get(key)
        return b.files if b else 0

    def add(
        self,
        key: int,
        bot: Bot,
        message: Message,
        piece: Awaitable[Piece] | Piece,
        *,
        instruction: bool = False,
        voice: bool = False,
        files: int = 0,
    ) -> None:
        batch = self._batches.get(key)
        if batch is None:
            batch = Batch(bot=bot, message=message)
            self._batches[key] = batch
        if isinstance(piece, Piece):
            fut: asyncio.Future[Piece] = asyncio.get_running_loop().create_future()
            fut.set_result(piece)
        else:
            fut = asyncio.ensure_future(piece)  # обработка стартует сразу
        batch.pieces.append(fut)
        batch.message = message
        batch.has_instruction |= instruction
        batch.voice |= voice
        batch.files += files
        if batch.timer is not None:
            batch.timer.cancel()
        delay = self._text_wait if batch.has_instruction else self._media_wait
        batch.timer = asyncio.get_running_loop().call_later(delay, self._fire, key)

    def _fire(self, key: int) -> None:
        batch = self._batches.pop(key, None)
        if batch is None:
            return
        task = asyncio.create_task(self._safe_run(batch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_run(self, batch: Batch) -> None:
        try:
            await self._run(batch)
        except Exception:  # noqa: BLE001 — ошибка одной задачи не должна ломать бота
            log.exception("batch processing failed")
            try:
                from pai.bot import texts

                await batch.message.answer(texts.INTERNAL_ERROR)
            except Exception:  # noqa: BLE001
                pass

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Дождаться, пока все собранные задачи обработаны (тесты, остановка бота)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._batches or self._tasks:
            if loop.time() > deadline:
                raise TimeoutError("collector is still busy")
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        for batch in self._batches.values():
            if batch.timer is not None:
                batch.timer.cancel()
            for p in batch.pieces:
                p.cancel()
        self._batches.clear()
        for t in list(self._tasks):
            t.cancel()
