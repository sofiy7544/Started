"""Голос: подготовка текста и звука, движки (формат запросов), Voice Engine (бюджет/лимиты/ошибки),
сценарии в Telegram. Без платных вызовов: OpenAI — локальный фейковый сервер, остальное — подмены."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import openai
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from sqlalchemy import select

from pai.bot import texts
from pai.bot.app import AppContext, build_dispatcher
from pai.db.models import UsageRecord
from pai.services.budget import BudgetGuard
from pai.voice.audio import duration_seconds, is_ogg_opus, to_ogg_opus
from pai.voice.engine import VoiceEngine, VoiceLimitError
from pai.voice.providers import (
    EdgeTTS,
    LocalWhisperSTT,
    OpenAISTT,
    OpenAITTS,
    Speech,
    Transcript,
    VoiceError,
    VoiceOption,
)
from pai.voice.text import prepare_for_speech, truncate_for_speech
from tests.conftest import OWNER, STRANGER, make_audio
from tests.fakes import FakeTelegram

# ---- подготовка текста и звука --------------------------------------------------------------


def test_text_is_prepared_for_natural_speech() -> None:
    out = prepare_for_speech("**Итог:**\n• Встреча в 10:00 🙂\n- Ссылка: https://example.com/x\n1. Отчёт в четверг")
    assert out == "Итог: Встреча в 10:00. Ссылка: ссылка в тексте сообщения. Отчёт в четверг."
    assert "*" not in out and "http" not in out and "🙂" not in out


def test_truncation_keeps_whole_sentences() -> None:
    text = "Первое предложение. Второе предложение. Третье очень длинное предложение про всё."
    cut, truncated = truncate_for_speech(text, 45)
    assert truncated and cut == "Первое предложение. Второе предложение."
    assert truncate_for_speech("коротко", 100) == ("коротко", False)


def test_audio_conversion_to_telegram_voice_format() -> None:
    mp3 = make_audio(1.5, "mp3")
    ogg = to_ogg_opus(mp3)
    assert not is_ogg_opus(mp3) and is_ogg_opus(ogg)
    assert abs(duration_seconds(ogg) - 1.5) < 0.2
    already = make_audio(1.0, "ogg")
    assert to_ogg_opus(already) is already  # OGG/Opus не перекодируется повторно


# ---- OpenAI: формат запросов на фейковом сервере -------------------------------------------


class FakeOpenAIAudio:
    def __init__(self) -> None:
        self.speech: list[dict] = []
        self.transcriptions: list[dict] = []
        self.usage: dict | None = {"type": "tokens", "input_tokens": 120, "output_tokens": 20,
                                   "input_token_details": {"audio_tokens": 100, "text_tokens": 20}}
        self.status = 200
        self.audio = make_audio(2.0, "ogg")

    async def _speech(self, request: web.Request) -> web.Response:
        self.speech.append(await request.json())
        if self.status != 200:
            return web.json_response({"error": {"message": "x", "type": "server_error"}}, status=self.status)
        return web.Response(body=self.audio, content_type="audio/ogg")

    async def _transcribe(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.transcriptions.append({k: (v if isinstance(v, str) else v.filename) for k, v in form.items()})
        body = {"text": "Привет, это тест."}
        if self.usage:
            body["usage"] = self.usage
        return web.json_response(body)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/audio/speech", self._speech)
        app.router.add_post("/v1/audio/transcriptions", self._transcribe)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"


@pytest.fixture
async def oa_audio():
    fake = FakeOpenAIAudio()
    url = await fake.start()
    client = openai.AsyncOpenAI(api_key="sk-test", base_url=url, max_retries=0)
    yield fake, client
    await client.close()
    await fake.runner.cleanup()


async def test_openai_tts_request_format(oa_audio) -> None:
    fake, client = oa_audio
    speech = await OpenAITTS(client=client).synthesize("Привет", voice="cedar", speed=1.15, style="calm")
    req = fake.speech[0]
    assert req["model"] == "gpt-4o-mini-tts" and req["voice"] == "cedar" and req["speed"] == 1.15
    assert req["response_format"] == "opus" and "спокойно" in req["instructions"]
    assert is_ogg_opus(speech.ogg_opus) and abs(speech.seconds - 2.0) < 0.2
    assert not speech.exact_cost and speech.cost_usd > 0  # API не отдаёт usage — оценка по длительности


async def test_openai_stt_uses_exact_usage_cost(oa_audio) -> None:
    fake, client = oa_audio
    result = await OpenAISTT(client=client).transcribe(make_audio(2.0), "voice.ogg")
    assert result.text == "Привет, это тест." and result.exact_cost
    # 100 аудио-токенов × $3 + 20 текст × $1.25 + 20 выход × $5 (за 1M)
    assert result.cost_usd == Decimal("0.000425")
    form = fake.transcriptions[0]
    assert form["model"] == "gpt-4o-mini-transcribe" and form["file"] == "voice.ogg"
    fake.usage = None
    approx = await OpenAISTT(client=client).transcribe(make_audio(2.0), "voice.ogg")
    assert not approx.exact_cost and approx.cost_usd > 0


async def test_openai_errors_are_classified(oa_audio) -> None:
    fake, client = oa_audio
    fake.status = 500
    with pytest.raises(VoiceError) as err:
        await OpenAITTS(client=client).synthesize("x", voice="marin", speed=1.0, style="natural")
    assert err.value.temporary


# ---- локальные движки (с подменой тяжёлых библиотек) ----------------------------------------


async def test_local_whisper_gets_decoded_audio_not_a_file(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeWhisper:
        def transcribe(self, audio, **kw):
            seen["type"], seen["len"] = type(audio).__name__, len(audio)
            seg = type("S", (), {"text": " Привіт, як справи? "})()
            return iter([seg]), type("I", (), {"language": "uk"})()

    stt = LocalWhisperSTT(model="small")
    monkeypatch.setattr(stt, "_load", lambda: FakeWhisper())
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    result = await stt.transcribe(make_audio(2.0), "voice.ogg")
    assert result.text == "Привіт, як справи?" and result.language == "uk" and result.cost_usd == 0
    assert seen["type"] == "ndarray" and abs(seen["len"] - 32_000) < 400  # 16 кГц × 2 с
    assert list(tmp_path.iterdir()) == []  # голос не сохраняется на диск


async def test_edge_tts_output_is_converted_to_ogg(monkeypatch) -> None:
    import edge_tts

    mp3 = make_audio(1.0, "mp3")
    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kw):
            seen.update(text=text, voice=voice, rate=kw.get("rate"))

        async def stream(self):
            yield {"type": "audio", "data": mp3}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    speech = await EdgeTTS().synthesize("Привіт", voice="uk-UA-PolinaNeural", speed=1.15, style="calm")
    assert is_ogg_opus(speech.ogg_opus) and speech.cost_usd == 0
    assert seen == {"text": "Привіт", "voice": "uk-UA-PolinaNeural", "rate": "+15%"}


# ---- Voice Engine ---------------------------------------------------------------------------


class FakeSTT:
    provider, model = "openai", "fake-stt"

    def __init__(self) -> None:
        self.calls = 0
        self.text = "Распознанный текст"
        self.error: Exception | None = None
        self.delay = 0.0

    def estimate_usd(self, seconds: float) -> Decimal:
        return Decimal("0.001")

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return Transcript(self.text, "ru", 3.0, Decimal("0.0002"), True)


class FakeTTS:
    provider, model = "openai", "fake-tts"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def voices(self) -> list[VoiceOption]:
        return [VoiceOption("marin", "Марин — мягкий"), VoiceOption("cedar", "Кедр — спокойный")]

    def default_voice(self) -> str:
        return "marin"

    def estimate_usd(self, chars: int) -> Decimal:
        return Decimal("0.002")

    async def synthesize(self, text: str, *, voice: str, speed: float, style: str) -> Speech:
        self.calls.append({"text": text, "voice": voice, "speed": speed, "style": style})
        if self.error:
            raise self.error
        return Speech(make_audio(1.0), 1.0, Decimal("0.0015"), False)


@pytest.fixture
def voice_engine(settings, sessions):
    budget = BudgetGuard(sessions, monthly_limit_usd=settings.monthly_budget_usd)
    return VoiceEngine(settings=settings, budget=budget, stt=FakeSTT(), tts=FakeTTS())


async def usage_rows(sessions) -> list[tuple[str, Decimal, bool]]:
    async with sessions() as s:
        return [(r.operation, r.cost_usd, r.confirmed) for r in await s.scalars(select(UsageRecord))]


async def test_engine_records_costs_and_releases_reserve(voice_engine, assistant, sessions) -> None:
    user = await assistant.get_user(OWNER)
    await voice_engine.transcribe(user.id, b"audio", "v.ogg", 3.0)
    spoken = await voice_engine.speak(user, "Привет!")
    assert not spoken.truncated
    assert await usage_rows(sessions) == [("stt", Decimal("0.0002"), True), ("tts", Decimal("0.0015"), False)]
    assert voice_engine._budget.reserved == 0


async def test_engine_limits_without_calling_api(voice_engine, assistant, sessions, settings) -> None:
    user = await assistant.get_user(OWNER)
    with pytest.raises(VoiceLimitError):
        await voice_engine.transcribe(user.id, b"a", "v.ogg", settings.max_voice_seconds + 1)
    assert voice_engine.stt.calls == 0
    broke = VoiceEngine(settings=settings, budget=BudgetGuard(sessions, monthly_limit_usd=0),
                        stt=FakeSTT(), tts=FakeTTS())
    with pytest.raises(VoiceLimitError, match="бюджет"):
        await broke.transcribe(user.id, b"a", "v.ogg", 3.0)
    with pytest.raises(VoiceLimitError, match="бюджет"):
        await broke.speak(user, "Привет")
    assert broke.stt.calls == 0 and broke.tts.calls == []


async def test_engine_failures_and_cancellation_accounting(voice_engine, assistant, sessions) -> None:
    user = await assistant.get_user(OWNER)
    voice_engine.stt.error = VoiceError("503")
    with pytest.raises(VoiceError):
        await voice_engine.transcribe(user.id, b"a", "v.ogg", 3.0)
    assert await usage_rows(sessions) == []  # HTTP-ошибка — без расхода
    voice_engine.stt.error, voice_engine.stt.delay = None, 5
    task = asyncio.create_task(voice_engine.transcribe(user.id, b"a", "v.ogg", 3.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await usage_rows(sessions)
    assert rows[0][0] == "stt" and rows[0][2] is False  # отмена — верхняя оценка
    assert voice_engine._budget.reserved == 0


async def test_long_answers_are_truncated_for_speech(make_assistant, sessions, settings) -> None:
    eng = VoiceEngine(settings=settings.model_copy(update={"max_tts_chars": 120}),
                      budget=BudgetGuard(sessions, monthly_limit_usd=10), stt=FakeSTT(), tts=FakeTTS())
    a = make_assistant()
    user = await a.get_user(OWNER)
    spoken = await eng.speak(user, "Первое предложение ответа. " * 20)
    assert spoken.truncated and len(eng.tts.calls[0]["text"]) <= 120


async def test_voice_preferences(voice_engine, assistant) -> None:
    user = await assistant.get_user(OWNER)
    assert voice_engine.voice_for(user) == "marin"
    user.tts_voice = "нет-такого"
    assert voice_engine.voice_for(user) == "marin"  # неизвестный голос → по умолчанию
    assert voice_engine.wants_voice_reply(user, incoming_voice=True)
    assert not voice_engine.wants_voice_reply(user, incoming_voice=False)
    user.voice_mode = "always"
    assert voice_engine.wants_voice_reply(user, incoming_voice=False)
    user.voice_mode = "never"
    assert not voice_engine.wants_voice_reply(user, incoming_voice=True)


# ---- Telegram ------------------------------------------------------------------------------


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def vbot(tg, assistant, settings, sessions, voice_engine):
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(settings, assistant, sessions, voice_engine))

    async def feed() -> list[tuple[str, dict]]:
        before = len(tg.calls)
        await dp.feed_raw_update(bot, tg.updates.get_nowait())
        await assistant.memory.wait()
        return tg.calls[before:]

    yield feed, tg, voice_engine
    await bot.session.close()


def texts_of(calls) -> list[str]:
    return [p.get("text") or p.get("caption", "") for m, p in calls
            if m in ("sendMessage", "editMessageText", "sendVoice")]


async def test_voice_message_full_cycle(vbot, script) -> None:
    feed, tg, eng = vbot
    eng.stt.text = "Какие у меня планы на завтра?"
    script.replies = ["Завтра в 10 созвон с командой."]
    tg.push_voice(OWNER, make_audio(2.0), duration=2)
    calls = await feed()
    methods = [m for m, _ in calls]
    assert "sendChatAction" in methods
    assert texts_of(calls) == [texts.heard("Какие у меня планы на завтра?"), "Завтра в 10 созвон с командой."]
    voice_call = next(p for m, p in calls if m == "sendVoice")
    assert voice_call["caption"] == "Завтра в 10 созвон с командой."  # голос + текст подписью
    assert script.seen[-1].user_prompts[-1] == "Какие у меня планы на завтра?"
    assert eng.tts.calls[0]["text"] == "Завтра в 10 созвон с командой."


async def test_text_mode_and_always_mode(vbot, assistant, script) -> None:
    feed, tg, eng = vbot
    tg.push_text(OWNER, "привет")
    calls = await feed()
    assert "sendVoice" not in [m for m, _ in calls]  # на текст — текстом (режим auto)
    user = await assistant.get_user(OWNER)
    await assistant.set_voice(user, voice_mode="always")
    tg.push_text(OWNER, "ещё вопрос")
    assert "sendVoice" in [m for m, _ in await feed()]
    await assistant.set_voice(user, voice_mode="never")
    tg.push_voice(OWNER, make_audio(1.0), duration=1)
    calls = await feed()
    assert "sendVoice" not in [m for m, _ in calls] and len(texts_of(calls)) == 2


async def test_too_long_empty_and_failed_voice(vbot, script, settings) -> None:
    feed, tg, eng = vbot
    tg.push_voice(OWNER, make_audio(1.0), duration=settings.max_voice_seconds + 10)
    assert texts_of(await feed()) == [texts.VOICE_TOO_LONG.format(limit=settings.max_voice_seconds)]
    assert eng.stt.calls == 0  # не скачивали и не распознавали
    eng.stt.text = "   "
    tg.push_voice(OWNER, make_audio(1.0), duration=1)
    assert texts_of(await feed()) == [texts.VOICE_NOT_HEARD]
    eng.stt.error = VoiceError("503")
    tg.push_voice(OWNER, make_audio(1.0), duration=1)
    assert texts_of(await feed()) == [texts.VOICE_FAILED]
    assert script.seen == []  # модель не вызывалась


async def test_tts_failure_falls_back_to_text(vbot, script) -> None:
    feed, tg, eng = vbot
    eng.tts.error = VoiceError("tts down")
    script.replies = ["Ответ, который должен дойти."]
    tg.push_voice(OWNER, make_audio(1.0), duration=1)
    out = texts_of(await feed())
    assert texts.TTS_FAILED in out and out[-1] == "Ответ, который должен дойти."


async def test_audio_and_video_note_are_transcribed(vbot) -> None:
    feed, tg, eng = vbot
    tg.push_voice(OWNER, make_audio(1.0, "mp3"), duration=1, kind="audio")
    await feed()
    tg.push_voice(OWNER, make_audio(1.0), duration=1, kind="video_note")
    await feed()
    assert eng.stt.calls == 2


async def test_stranger_voice_is_ignored(vbot) -> None:
    feed, tg, eng = vbot
    tg.push_voice(STRANGER, make_audio(1.0), duration=1)
    assert texts_of(await feed()) == [texts.ACCESS_DENIED]
    assert eng.stt.calls == 0


async def test_voice_settings_screen(vbot, assistant) -> None:
    feed, tg, eng = vbot
    tg.push_text(OWNER, texts.BTN_VOICE)
    calls = await feed()
    assert "Отвечать: голосом на голосовые" in texts_of(calls)[0]
    markup = json.loads(next(p for m, p in reversed(calls) if "reply_markup" in p)["reply_markup"])
    buttons = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert {"vm:always", "vv:cedar", "vs:1.15", "vsample"} <= buttons
    for data in ("vm:always", "vv:cedar", "vs:1.15", "vv:hacker", "vs:99", "vm:root"):
        tg.push_callback(OWNER, data)
        await feed()
    user = await assistant.get_user(OWNER)
    assert (user.voice_mode, user.tts_voice, user.tts_speed) == ("always", "cedar", 1.15)  # мусор отброшен
    tg.push_callback(OWNER, "vsample")
    assert "sendVoice" in [m for m, _ in await feed()]
    assert eng.tts.calls[-1]["voice"] == "cedar" and eng.tts.calls[-1]["speed"] == 1.15
