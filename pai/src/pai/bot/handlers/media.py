"""Фото, документы, видео, аудиофайлы, архивы: приём, обработка, ответ по существу.

Команды не нужны: прислали файл (с подписью или без), голосом или текстом сказали, что сделать, —
сборщик объединит сообщения в одну задачу.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from pai.bot import texts
from pai.bot.app import AppContext, OwnerOnly
from pai.bot.collector import Batch, Collector, Piece
from pai.media.files import FileTooBig, IncomingFile, download, files_of
from pai.media.safety import safe_name

log = logging.getLogger(__name__)


async def _process(bot: Bot, app: AppContext, user_id: int, files: list[IncomingFile], caption: str) -> Piece:
    media = app.media
    piece = Piece(text=caption)
    for f in files:
        name = safe_name(f.name)
        tmp = media.tmp_path()
        try:
            await download(bot, f, tmp, media.max_file_bytes)
        except FileTooBig:
            tmp.unlink(missing_ok=True)
            reason = texts.file_too_big(media.max_file_bytes, bool(app.settings.telegram_api_url))
            piece.rejected.append(f"{name} — {reason}")
            continue
        except (TelegramAPIError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            log.warning("download failed: %s", type(exc).__name__)
            piece.rejected.append(f"{name} — не удалось скачать из Telegram, пришлите ещё раз")
            continue
        result = await media.ingest(user_id, name, tmp)
        piece.attachments += result.attachments
        piece.rejected += result.rejected
    return piece


async def on_media(message: Message, bot: Bot, app: AppContext, collector: Collector) -> None:
    files = files_of(message)
    if not files:
        return
    u = message.from_user
    user = await app.assistant.get_user(u.id, u.first_name)
    caption = (message.caption or "").strip()
    room = app.settings.max_files_per_message - collector.files_in_batch(u.id)
    extra = files[max(room, 0):]
    files = files[: max(room, 0)]
    if extra:
        piece = Piece(text=caption, rejected=[f"{safe_name(f.name)} — {texts.TOO_MANY_FILES}" for f in extra])
        collector.add(u.id, bot, message, piece, instruction=bool(caption))
    if files:
        collector.add(u.id, bot, message, _process(bot, app, user.id, files, "" if extra else caption),
                      instruction=bool(caption), files=len(files))


async def run_batch(app: AppContext, batch: Batch) -> None:
    """Собранная задача: дождаться обработки файлов → отчёт о получении → ответ модели."""
    from pai.bot.handlers.voice import send_answer

    message, bot = batch.message, batch.bot
    u = message.from_user
    user = await app.assistant.get_user(u.id, u.first_name)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        pieces: list[Piece] = []
        for fut in batch.pieces:
            try:
                pieces.append(await fut)
            except Exception:  # noqa: BLE001
                log.exception("media piece failed")
                pieces.append(Piece(rejected=["файл — не удалось обработать (ошибка записана в лог)"]))
        instruction = "\n".join(p.text for p in pieces if p.text).strip()
        views = [v for p in pieces for v in p.attachments]
        rejected = [r for p in pieces for r in p.rejected]
        if texts.needs_receipt(views, rejected):
            await message.answer(texts.media_receipt(views, rejected))
        if not views:
            if not instruction:
                return
            if rejected:
                instruction += "\n(" + texts.FILES_FAILED_NOTE + "; ".join(rejected) + ")"
            answer = await app.assistant.ask(user, instruction)
        else:
            prepared = await app.media.context.prepare(user.id, views, rejected)
            answer = await app.assistant.ask(user, instruction, prepared)
    await send_answer(message, bot, app, user, answer, incoming_voice=batch.voice)


def create_router() -> Router:
    r = Router(name="media")
    r.message.filter(F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.callback_query.filter(OwnerOnly())
    r.message.register(on_media, F.photo | F.document | F.video | F.animation | F.audio)
    return r
