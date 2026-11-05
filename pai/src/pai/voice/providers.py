"""Движки распознавания (STT) и синтеза (TTS) речи с единым интерфейсом.

STT: openai (gpt-4o-mini-transcribe) | local (faster-whisper, бесплатно, на вашем компьютере)
TTS: openai (gpt-4o-mini-tts — тембр, скорость, манера) | edge (Microsoft Edge TTS, бесплатно)
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pai.voice.audio import decode_pcm_16k, duration_seconds, to_ogg_opus

log = logging.getLogger(__name__)
_MILLION = Decimal(1_000_000)


class VoiceError(RuntimeError):
    """Ошибка голосового движка. temporary — имеет смысл повторить позже."""

    def __init__(self, message: str, *, temporary: bool = True, billed_usd: Decimal | None = None) -> None:
        super().__init__(message)
        self.temporary = temporary
        self.billed_usd = billed_usd


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    seconds: float
    cost_usd: Decimal
    exact_cost: bool  # True — по usage провайдера, False — оценка


@dataclass(frozen=True)
class Speech:
    ogg_opus: bytes
    seconds: float
    cost_usd: Decimal
    exact_cost: bool


@dataclass(frozen=True)
class VoiceOption:
    id: str
    title: str


class SpeechToText(Protocol):
    provider: str
    model: str

    def estimate_usd(self, seconds: float) -> Decimal: ...

    async def transcribe(self, audio: bytes, filename: str) -> Transcript: ...


class TextToSpeech(Protocol):
    provider: str
    model: str

    def voices(self) -> list[VoiceOption]: ...

    def default_voice(self) -> str: ...

    def estimate_usd(self, chars: int) -> Decimal: ...

    async def synthesize(self, text: str, *, voice: str, speed: float, style: str) -> Speech: ...


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _openai_error(exc: Exception) -> VoiceError:
    import openai

    temporary = isinstance(
        exc, (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
    )
    status = getattr(exc, "status_code", None)
    return VoiceError(f"{type(exc).__name__} status={status}", temporary=temporary)


# ---------------------------------------------------------------- OpenAI -------------------------

STT_AUDIO_USD_PER_MTOK = Decimal("3.00")  # gpt-4o-mini-transcribe, аудиовход (genai-prices)
STT_TEXT_IN_USD_PER_MTOK = Decimal("1.25")
STT_TEXT_OUT_USD_PER_MTOK = Decimal("5.00")
STT_USD_PER_MINUTE_ESTIMATE = Decimal("0.003")
TTS_TEXT_USD_PER_MTOK = Decimal("0.60")  # gpt-4o-mini-tts, текст на входе
TTS_USD_PER_MINUTE_ESTIMATE = Decimal("0.015")  # звук на выходе ≈ 1250 токенов/мин × $12/1M


class OpenAISTT:
    provider = "openai"

    def __init__(
        self, *, model: str = "gpt-4o-mini-transcribe", timeout: float = 60, max_retries: int = 2, client=None
    ) -> None:  # type: ignore[no-untyped-def]
        import openai

        self.model = model
        self._client = client or openai.AsyncOpenAI(timeout=timeout, max_retries=max_retries)

    def estimate_usd(self, seconds: float) -> Decimal:
        return _q(STT_USD_PER_MINUTE_ESTIMATE * Decimal(str(max(seconds, 1.0) / 60)) * 2)  # с запасом ×2

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        import openai

        seconds = await asyncio.to_thread(duration_seconds, audio)
        try:
            r = await self._client.audio.transcriptions.create(
                model=self.model,
                file=(filename, audio),
                prompt="Разговор на русском или украинском языке. Расставь знаки препинания.",
            )
        except openai.APIError as exc:
            raise _openai_error(exc) from exc
        usage = getattr(r, "usage", None)
        if usage is not None and getattr(usage, "input_tokens", None) is not None:
            details = getattr(usage, "input_token_details", None)
            audio_tokens = getattr(details, "audio_tokens", None) or 0
            text_tokens = getattr(details, "text_tokens", None) or max(usage.input_tokens - audio_tokens, 0)
            cost = (
                STT_AUDIO_USD_PER_MTOK * audio_tokens
                + STT_TEXT_IN_USD_PER_MTOK * text_tokens
                + STT_TEXT_OUT_USD_PER_MTOK * (usage.output_tokens or 0)
            ) / _MILLION
            return Transcript(r.text.strip(), None, seconds, _q(cost), True)
        return Transcript(
            r.text.strip(), None, seconds, _q(STT_USD_PER_MINUTE_ESTIMATE * Decimal(str(seconds / 60))), False
        )


OPENAI_VOICES = [
    VoiceOption("marin", "Марин — мягкий, естественный"),
    VoiceOption("cedar", "Кедр — спокойный, глубокий"),
    VoiceOption("coral", "Корал — тёплый, живой"),
    VoiceOption("sage", "Сейдж — ровный, деловой"),
    VoiceOption("ash", "Эш — бодрый"),
    VoiceOption("onyx", "Оникс — низкий"),
]

STYLE_INSTRUCTIONS = {
    "natural": "Говори естественно, как живой человек в разговоре: живая интонация, паузы на знаках "
    "препинания.",
    "calm": "Говори спокойно и неторопливо, мягким тоном, с чёткими паузами.",
    "friendly": "Говори дружелюбно и тепло, с лёгкой улыбкой в голосе.",
    "energetic": "Говори бодро и энергично, с выразительной интонацией.",
    "business": "Говори чётко и по-деловому, уверенно, без лишних эмоций.",
}


class OpenAITTS:
    provider = "openai"

    def __init__(
        self, *, model: str = "gpt-4o-mini-tts", timeout: float = 60, max_retries: int = 2, client=None
    ) -> None:  # type: ignore[no-untyped-def]
        import openai

        self.model = model
        self._client = client or openai.AsyncOpenAI(timeout=timeout, max_retries=max_retries)

    def voices(self) -> list[VoiceOption]:
        return OPENAI_VOICES

    def default_voice(self) -> str:
        return "marin"

    def estimate_usd(self, chars: int) -> Decimal:
        minutes = Decimal(chars) / Decimal(800)  # ≈ 800 символов русской речи в минуту
        return _q((TTS_USD_PER_MINUTE_ESTIMATE * minutes + TTS_TEXT_USD_PER_MTOK * chars / _MILLION) * 2)

    async def synthesize(self, text: str, *, voice: str, speed: float, style: str) -> Speech:
        import openai

        try:
            r = await self._client.audio.speech.create(
                model=self.model,
                voice=voice,
                input=text,
                response_format="opus",
                speed=speed,
                instructions=STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["natural"]),
            )
        except openai.APIError as exc:
            raise _openai_error(exc) from exc
        data = r.content
        seconds = await asyncio.to_thread(duration_seconds, data)
        cost = (
            TTS_USD_PER_MINUTE_ESTIMATE * Decimal(str(seconds / 60))
            + TTS_TEXT_USD_PER_MTOK * len(text) / _MILLION
        )
        return Speech(data, seconds, _q(cost), False)  # API не возвращает usage — оценка по длительности


# ---------------------------------------------------------------- локальные / бесплатные --------


class LocalWhisperSTT:
    """faster-whisper на вашем компьютере: бесплатно, данные никуда не уходят.
    Модель скачивается с Hugging Face при первом использовании (small ≈ 0.5 ГБ)."""

    provider = "local"

    def __init__(self, *, model: str = "small", device: str = "auto", compute_type: str = "int8") -> None:
        self.model = f"faster-whisper-{model}"
        self._size, self._device, self._compute = model, device, compute_type
        self._whisper = None
        self._lock = asyncio.Lock()

    def estimate_usd(self, seconds: float) -> Decimal:
        return Decimal(0)

    def _load(self):  # type: ignore[no-untyped-def]
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceError(
                "faster-whisper не установлен: uv sync --extra local-voice", temporary=False
            ) from exc
        return WhisperModel(self._size, device=self._device, compute_type=self._compute)

    def _run(self, audio: bytes) -> tuple[str, str | None, float]:
        pcm = decode_pcm_16k(audio)
        segments, info = self._whisper.transcribe(pcm, beam_size=5, vad_filter=True)  # type: ignore[union-attr]
        return " ".join(s.text.strip() for s in segments).strip(), info.language, len(pcm) / 16_000

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        async with self._lock:  # одна модель в памяти, по одному распознаванию за раз
            if self._whisper is None:
                self._whisper = await asyncio.to_thread(self._load)
            # Звук обрабатывается в памяти — временных файлов с голосом на диске нет.
            text, language, seconds = await asyncio.to_thread(self._run, audio)
        return Transcript(text, language, seconds, Decimal(0), True)


EDGE_VOICES = [
    VoiceOption("ru-RU-SvetlanaNeural", "Светлана (рус.)"),
    VoiceOption("ru-RU-DmitryNeural", "Дмитрий (рус.)"),
    VoiceOption("uk-UA-PolinaNeural", "Поліна (укр.)"),
    VoiceOption("uk-UA-OstapNeural", "Остап (укр.)"),
]


class EdgeTTS:
    """Бесплатный синтез Microsoft Edge (неофициальный сервис: может измениться без предупреждения).
    Тембр задаётся выбором голоса, манера речи не настраивается."""

    provider = "edge"
    model = "edge-tts"

    def voices(self) -> list[VoiceOption]:
        return EDGE_VOICES

    def default_voice(self) -> str:
        return "ru-RU-SvetlanaNeural"

    def estimate_usd(self, chars: int) -> Decimal:
        return Decimal(0)

    async def synthesize(self, text: str, *, voice: str, speed: float, style: str) -> Speech:
        try:
            import aiohttp
            import certifi
            import edge_tts
        except ImportError as exc:
            raise VoiceError("edge-tts не установлен: uv sync --extra local-voice", temporary=False) from exc
        ctx = ssl.create_default_context(cafile=certifi.where())
        if os.environ.get("SSL_CERT_FILE") and os.path.isfile(os.environ["SSL_CERT_FILE"]):
            ctx.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"])
        rate = f"{round((speed - 1) * 100):+d}%"
        comm = edge_tts.Communicate(
            text,
            voice,
            rate=rate,
            proxy=os.environ.get("HTTPS_PROXY"),
            connector=aiohttp.TCPConnector(ssl=ctx),
        )
        mp3 = b""
        try:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    mp3 += chunk["data"]
        except Exception as exc:  # noqa: BLE001 — библиотека бросает свои и сетевые исключения
            raise VoiceError(f"edge-tts: {type(exc).__name__}") from exc
        data = await asyncio.to_thread(to_ogg_opus, mp3)
        return Speech(data, await asyncio.to_thread(duration_seconds, data), Decimal(0), True)
