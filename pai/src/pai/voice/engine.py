"""Voice Engine: голос → текст и текст → голос с учётом бюджета, лимитов и настроек владельца."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pai.config import Settings
from pai.db.models import User
from pai.services.budget import BudgetGuard
from pai.voice.providers import (
    STYLE_INSTRUCTIONS,
    EdgeTTS,
    LocalWhisperSTT,
    OpenAISTT,
    OpenAITTS,
    Speech,
    SpeechToText,
    TextToSpeech,
    Transcript,
    VoiceError,
)
from pai.voice.text import prepare_for_speech, truncate_for_speech

log = logging.getLogger(__name__)

VOICE_MODES = ("auto", "always", "never")
SPEEDS = (0.9, 1.0, 1.15, 1.3)


class VoiceLimitError(VoiceError):
    """Голосовое длиннее разрешённого или бюджет исчерпан — без обращения к API."""

    def __init__(self, message: str) -> None:
        super().__init__(message, temporary=False)


@dataclass(frozen=True)
class SpokenAnswer:
    speech: Speech
    truncated: bool  # озвучено не всё — полный текст отправляется сообщением


class VoiceEngine:
    def __init__(
        self, *, settings: Settings, budget: BudgetGuard, stt: SpeechToText, tts: TextToSpeech | None
    ) -> None:
        self._settings = settings
        self._budget = budget
        self.stt = stt
        self.tts = tts

    # ---- настройки ---------------------------------------------------------------------

    @property
    def supports_style(self) -> bool:
        return isinstance(self.tts, OpenAITTS)

    def voice_for(self, user: User) -> str:
        if self.tts is None:
            return ""
        ids = {v.id for v in self.tts.voices()}
        return user.tts_voice if user.tts_voice in ids else self.tts.default_voice()

    def wants_voice_reply(self, user: User, *, incoming_voice: bool) -> bool:
        if self.tts is None or user.voice_mode == "never":
            return False
        return user.voice_mode == "always" or incoming_voice

    # ---- голос → текст ----------------------------------------------------------------

    async def transcribe(self, user_id: int, audio: bytes, filename: str, duration: float) -> Transcript:
        limit = self._settings.max_voice_seconds
        if duration > limit:
            raise VoiceLimitError(f"голосовое длиннее {limit} с")
        reservation = await self._budget.reserve_amount(
            self.stt.provider, self.stt.model, self.stt.estimate_usd(duration)
        )
        if reservation is None:
            raise VoiceLimitError("месячный бюджет исчерпан")
        try:
            result = await self.stt.transcribe(audio, filename)
        except VoiceError:
            self._budget.release(reservation)  # ответа с usage нет — расход не записываем
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._budget.record_unknown(reservation, user_id=user_id, operation="stt"))
            raise
        except BaseException:
            self._budget.release(reservation)
            raise
        await asyncio.shield(
            self._budget.record_cost(
                reservation, cost=result.cost_usd, exact=result.exact_cost, user_id=user_id, operation="stt"
            )
        )
        return result

    # ---- текст → голос ----------------------------------------------------------------

    async def speak(self, user: User, text: str) -> SpokenAnswer:
        if self.tts is None:
            raise VoiceLimitError("синтез речи выключен")
        prepared = prepare_for_speech(text)
        spoken, truncated = truncate_for_speech(prepared, self._settings.max_tts_chars)
        if not spoken:
            raise VoiceLimitError("нечего озвучивать")
        reservation = await self._budget.reserve_amount(
            self.tts.provider, self.tts.model, self.tts.estimate_usd(len(spoken))
        )
        if reservation is None:
            raise VoiceLimitError("месячный бюджет исчерпан")
        style = user.tts_style if user.tts_style in STYLE_INSTRUCTIONS else "natural"
        speed = user.tts_speed if 0.5 <= user.tts_speed <= 2.0 else 1.0
        try:
            speech = await self.tts.synthesize(spoken, voice=self.voice_for(user), speed=speed, style=style)
        except VoiceError:
            self._budget.release(reservation)
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._budget.record_unknown(reservation, user_id=user.id, operation="tts"))
            raise
        except BaseException:
            self._budget.release(reservation)
            raise
        await asyncio.shield(
            self._budget.record_cost(
                reservation, cost=speech.cost_usd, exact=speech.exact_cost, user_id=user.id, operation="tts"
            )
        )
        return SpokenAnswer(speech, truncated)


def build_voice(settings: Settings, budget: BudgetGuard) -> VoiceEngine | None:
    """Движки по настройкам: auto — облачный OpenAI при наличии ключа, иначе бесплатный локальный."""
    if not settings.voice_enabled:
        return None
    has_openai = settings.provider_available("openai")
    stt_kind = (
        settings.stt_provider if settings.stt_provider != "auto" else ("openai" if has_openai else "local")
    )
    tts_kind = (
        settings.tts_provider if settings.tts_provider != "auto" else ("openai" if has_openai else "edge")
    )
    if "openai" in (stt_kind, tts_kind) and not has_openai:
        raise ValueError("для голосового движка openai нужен OPENAI_API_KEY")
    timeout, retries = settings.llm_timeout_seconds, settings.llm_max_retries
    stt: SpeechToText = (
        OpenAISTT(timeout=timeout, max_retries=retries)
        if stt_kind == "openai"
        else LocalWhisperSTT(model=settings.stt_local_model)
    )
    tts: TextToSpeech | None = (
        OpenAITTS(timeout=timeout, max_retries=retries)
        if tts_kind == "openai"
        else EdgeTTS()
        if tts_kind == "edge"
        else None
    )
    log.info("голос: распознавание=%s, синтез=%s", stt.model, tts.model if tts else "выключен")
    return VoiceEngine(settings=settings, budget=budget, stt=stt, tts=tts)
