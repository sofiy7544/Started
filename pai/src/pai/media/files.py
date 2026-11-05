"""Telegram File Handler: что за файлы в сообщении и их скачивание во временный файл.

Ограничение Bot API: облачный Telegram отдаёт ботам файлы до 20 МБ. Больше — только через
собственный сервер Bot API (TELEGRAM_API_URL, режим --local) — тогда действует MAX_FILE_MB.
Фото Telegram сжимает; оригинальное качество — если отправить картинку «как файл».
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Message

TELEGRAM_CLOUD_LIMIT = 20 * 1024 * 1024


class FileTooBig(Exception):
    pass


@dataclass(frozen=True)
class IncomingFile:
    file_id: str
    name: str
    size: int | None
    kind: str  # photo | document | video | animation | audio


def files_of(message: Message) -> list[IncomingFile]:
    out: list[IncomingFile] = []
    if message.photo:
        best = max(message.photo, key=lambda p: (p.width * p.height, p.file_size or 0))  # самое крупное
        out.append(IncomingFile(best.file_id, f"photo_{message.message_id}.jpg", best.file_size, "photo"))
    if message.document:
        d = message.document
        out.append(IncomingFile(d.file_id, d.file_name or "file", d.file_size, "document"))
    if message.video:
        v = message.video
        out.append(IncomingFile(v.file_id, v.file_name or f"video_{message.message_id}.mp4", v.file_size, "video"))
    if message.animation and not message.document:
        a = message.animation
        out.append(IncomingFile(a.file_id, a.file_name or f"animation_{message.message_id}.mp4", a.file_size, "animation"))
    if message.audio:
        a = message.audio
        out.append(IncomingFile(a.file_id, a.file_name or f"audio_{message.message_id}.mp3", a.file_size, "audio"))
    return out


async def download(bot: Bot, f: IncomingFile, dest: Path, max_bytes: int) -> None:
    if f.size and f.size > max_bytes:
        raise FileTooBig
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):  # сеть иногда обрывается — повторяем, это бесплатно
        try:
            await bot.download(f.file_id, destination=dest, timeout=300)
            break
        except TelegramBadRequest as exc:
            if "too big" in str(exc).lower():
                raise FileTooBig from exc
            raise
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    if dest.stat().st_size > max_bytes:
        dest.unlink(missing_ok=True)
        raise FileTooBig
