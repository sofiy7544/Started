"""Голос: входящие голосовые/аудио/кружочки → текст → ответ (голосом и текстом); настройки голоса."""

from __future__ import annotations

import io
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from pai.bot import texts
from pai.bot.app import AppContext, OwnerOnly, split_message
from pai.bot.collector import Collector, Piece
from pai.services.assistant import Answer, Outcome
from pai.voice.engine import SPEEDS, VOICE_MODES, VoiceLimitError
from pai.voice.providers import STYLE_INSTRUCTIONS, VoiceError

log = logging.getLogger(__name__)
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API отдаёт ботам файлы до 20 МБ
LONG_VOICE_SECONDS = 150  # длиннее — распознаём частями по 2 минуты
CAPTION_LIMIT = 1024


async def send_answer(
    message: Message,
    bot: Bot,
    app: AppContext,
    user,
    answer: Answer,  # type: ignore[no-untyped-def]
    *,
    incoming_voice: bool,
) -> None:
    """Отправляет ответ: голосом (если так настроено и ответ успешный) и всегда — текстом.
    Если озвучить не удалось, ответ не теряется: он приходит текстом."""
    from pai.bot.handlers.reminders import send_cards

    if not answer.text:
        await send_cards(message, answer)
        await send_files(message, answer)
        return
    voice = app.voice
    if (
        answer.outcome is Outcome.OK
        and voice is not None
        and voice.wants_voice_reply(user, incoming_voice=incoming_voice)
    ):
        try:
            async with ChatActionSender.record_voice(bot=bot, chat_id=message.chat.id):
                spoken = await voice.speak(user, answer.text)
        except VoiceError as exc:
            log.warning("tts failed user=%s: %s", user.id, exc)
            await message.answer(texts.TTS_FAILED)
        else:
            file = BufferedInputFile(spoken.speech.ogg_opus, filename="answer.ogg")
            fits = len(answer.text) <= CAPTION_LIMIT and not spoken.truncated
            await message.answer_voice(file, caption=answer.text if fits else None)
            if fits:
                await send_cards(message, answer)
                await send_files(message, answer)
                return
            if spoken.truncated:
                await message.answer(texts.TTS_TRUNCATED)
    for part in split_message(answer.text):
        await message.answer(part)
    await send_cards(message, answer)
    await send_files(message, answer)


async def send_files(message: Message, answer: Answer) -> None:
    """Файлы, которые модель создала инструментом create_file (отчёт, таблица…)."""
    for f in answer.files:
        await message.answer_document(BufferedInputFile(f.data, filename=f.filename))


async def _transcribe(message: Message, bot: Bot, app: AppContext, user) -> str | None:  # type: ignore[no-untyped-def]
    """Голосовое → текст. Ошибки сообщает владельцу сам; None — текста нет.
    Длинные записи (больше 2 минут) распознаются по частям."""
    media = message.voice or message.audio or message.video_note
    duration = float(getattr(media, "duration", 0) or 0)
    limit = app.settings.max_voice_seconds
    buf = io.BytesIO()
    await bot.download(media, destination=buf)
    ext = ".ogg" if message.voice else ".mp4" if message.video_note else ".mp3"
    if duration <= 0 or duration > LONG_VOICE_SECONDS - 30:
        # Длительность от Telegram бывает не указана — меряем сами (бесплатно, локально).
        import asyncio

        from pai.voice.audio import duration_seconds

        try:
            duration = max(duration, await asyncio.to_thread(duration_seconds, buf.getvalue()))
        except Exception:  # noqa: BLE001 — не смогли измерить: распознаём целиком
            pass
        if duration > limit:
            await message.answer(texts.VOICE_TOO_LONG.format(limit=limit))
            return None
    try:
        if duration > LONG_VOICE_SECONDS:
            text = await _transcribe_long(app, user.id, buf.getvalue())
        else:
            text = (await app.voice.transcribe(user.id, buf.getvalue(), f"voice{ext}", duration)).text
    except VoiceLimitError as exc:
        await message.answer(
            texts.VOICE_BUDGET if "бюджет" in str(exc) else texts.VOICE_TOO_LONG.format(limit=limit)
        )
        return None
    except VoiceError as exc:
        log.warning("stt failed user=%s: %s", user.id, exc)
        await message.answer(texts.VOICE_FAILED)
        return None
    if not text.strip():
        await message.answer(texts.VOICE_NOT_HEARD)
        return None
    await message.reply(texts.heard(text))
    return text


async def _transcribe_long(app: AppContext, user_id: int, audio: bytes) -> str:
    import asyncio

    from pai.voice.audio import ogg_chunks

    parts = await asyncio.to_thread(ogg_chunks, audio, chunk_seconds=120.0,
                                    max_seconds=float(app.settings.max_voice_seconds))
    out = []
    for start, end, ogg in parts:
        out.append((await app.voice.transcribe(user_id, ogg, "part.ogg", end - start)).text.strip())
    return " ".join(t for t in out if t)


async def on_voice(message: Message, bot: Bot, app: AppContext, collector: Collector | None = None) -> None:
    if app.voice is None:
        await message.answer(texts.VOICE_DISABLED)
        return
    media = message.voice or message.audio or message.video_note
    u = message.from_user
    user = await app.assistant.get_user(u.id, u.first_name)
    duration = float(getattr(media, "duration", 0) or 0)
    limit = app.settings.max_voice_seconds
    if duration > limit or (media.file_size or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        await message.answer(texts.VOICE_TOO_LONG.format(limit=limit))
        return
    if collector is not None and collector.pending(u.id):
        # Голосовое после файлов — объяснение задачи к ним.
        async def piece() -> Piece:
            async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                return Piece(text=await _transcribe(message, bot, app, user) or "")

        collector.add(u.id, bot, message, piece(), instruction=True, voice=True)
        return
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        text = await _transcribe(message, bot, app, user)
        if text is None:
            return
        answer = await app.assistant.ask(user, text)
    await send_answer(message, bot, app, user, answer, incoming_voice=True)


# ---- настройки голоса --------------------------------------------------------------------


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def settings_view(app: AppContext, user) -> tuple[str, InlineKeyboardMarkup]:  # type: ignore[no-untyped-def]
    voice = app.voice
    tts = voice.tts
    current = voice.voice_for(user)
    options = tts.voices() if tts else []
    title = next((o.title for o in options if o.id == current), current or "—")
    text = texts.voice_settings_text(
        mode=user.voice_mode,
        voice_title=title,
        speed=user.tts_speed,
        style=user.tts_style if voice.supports_style else None,
        stt=voice.stt.model,
        tts=tts.model if tts else "выключен",
    )
    mark = lambda ok: "✅ " if ok else ""  # noqa: E731
    rows = [[(mark(m == user.voice_mode) + texts.VOICE_MODE_TITLES[m], f"vm:{m}") for m in VOICE_MODES]]
    if tts:
        pairs = [(mark(o.id == current) + o.title.split(" — ")[0], f"vv:{o.id}") for o in options]
        rows += [pairs[i : i + 2] for i in range(0, len(pairs), 2)]
        rows.append([(mark(abs(s - user.tts_speed) < 0.01) + f"{s:g}×", f"vs:{s}") for s in SPEEDS])
        if voice.supports_style:
            styles = [
                (mark(k == user.tts_style) + texts.STYLE_TITLES_VOICE[k], f"vt:{k}")
                for k in STYLE_INSTRUCTIONS
            ]
            rows += [styles[:3], styles[3:]]
        rows.append([("🔊 Пример", "vsample")])
    return text, _kb(rows)


async def cmd_voice(message: Message, app: AppContext) -> None:
    if app.voice is None:
        await message.answer(texts.VOICE_DISABLED)
        return
    user = await app.assistant.get_user(message.from_user.id, message.from_user.first_name)
    text, kb = settings_view(app, user)
    await message.answer(text, reply_markup=kb)


async def cb_voice_setting(callback: CallbackQuery, app: AppContext) -> None:
    if app.voice is None:
        await callback.answer()
        return
    user = await app.assistant.get_user(callback.from_user.id, callback.from_user.first_name)
    kind, _, value = (callback.data or "").partition(":")
    tts = app.voice.tts
    # Принимаются только значения из списков движка — подделанная кнопка ничего не сломает.
    if kind == "vm" and value in VOICE_MODES:
        await app.assistant.set_voice(user, voice_mode=value)
    elif kind == "vv" and tts and value in {v.id for v in tts.voices()}:
        await app.assistant.set_voice(user, tts_voice=value)
    elif kind == "vs" and value in {str(s) for s in SPEEDS}:
        await app.assistant.set_voice(user, tts_speed=float(value))
    elif kind == "vt" and value in STYLE_INSTRUCTIONS and app.voice.supports_style:
        await app.assistant.set_voice(user, tts_style=value)
    else:
        await callback.answer()
        return
    await callback.answer("Сохранено")
    if isinstance(callback.message, Message):
        text, kb = settings_view(app, user)
        await callback.message.edit_text(text, reply_markup=kb)


async def cb_sample(callback: CallbackQuery, bot: Bot, app: AppContext) -> None:
    await callback.answer()
    if app.voice is None or not isinstance(callback.message, Message):
        return
    user = await app.assistant.get_user(callback.from_user.id, callback.from_user.first_name)
    try:
        async with ChatActionSender.record_voice(bot=bot, chat_id=callback.message.chat.id):
            spoken = await app.voice.speak(user, texts.VOICE_SAMPLE_TEXT)
    except VoiceError:
        await callback.message.answer(texts.TTS_FAILED.strip("()"))
        return
    await callback.message.answer_voice(BufferedInputFile(spoken.speech.ogg_opus, filename="sample.ogg"))


def create_router() -> Router:
    r = Router(name="voice")
    r.message.filter(F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.callback_query.filter(OwnerOnly())
    r.message.register(cmd_voice, Command("voice"))
    r.message.register(cmd_voice, F.text == texts.BTN_VOICE)
    r.message.register(on_voice, F.voice | F.audio | F.video_note)
    r.callback_query.register(cb_sample, F.data == "vsample")
    r.callback_query.register(cb_voice_setting, F.data.regexp(r"^v[mvst]:"))
    return r
