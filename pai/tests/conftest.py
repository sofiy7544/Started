from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from pai.config import PROJECT_ROOT, Settings
from pai.db import engine as db
from pai.llm.catalog import load_catalog
from pai.llm.engine import PydanticAIEngine
from pai.memory.engine import MemoryEngine
from pai.memory.store import MemoryStore
from pai.reminders.service import ReminderService
from pai.services.assistant import Assistant
from pai.services.budget import BudgetGuard
from pai.services.limits import CircuitBreaker, RateLimiter

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class WallClock:
    """Управляемое «текущее время» для напоминаний и инициативы в тестах."""

    def __init__(self) -> None:
        self.now = NOW

    def advance(self, **kw) -> None:  # type: ignore[no-untyped-def]
        from datetime import timedelta

        self.now += timedelta(**kw)


CLOCK = WallClock()
OWNER = 111
STRANGER = 999


@dataclass
class Seen:
    model_name: str
    messages: list[ModelMessage]
    instructions: str | None

    @property
    def user_prompts(self) -> list[str]:
        return [p.content for m in self.messages if isinstance(m, ModelRequest)
                for p in m.parts if isinstance(p, UserPromptPart) and isinstance(p.content, str)]

    @property
    def assistant_texts(self) -> list[str]:
        return [p.content for m in self.messages if isinstance(m, ModelResponse)
                for p in m.parts if isinstance(p, TextPart)]


@dataclass
class Script:
    """Подставная модель: отвечает по сценарию и запоминает, что получила."""

    replies: list[str | Exception] = field(default_factory=list)
    seen: list[Seen] = field(default_factory=list)
    # Запросы памяти (структурированный ответ) — отдельно, чтобы не смешивались с чатом.
    memory_replies: list[dict | Exception] = field(default_factory=list)
    memory_seen: list[Seen] = field(default_factory=list)
    input_tokens: int = 1000
    output_tokens: int = 100

    async def fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        usage = RequestUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        if info.output_tools:
            self.memory_seen.append(Seen("", list(messages), info.instructions))
            item = self.memory_replies.pop(0) if self.memory_replies else {"episode_summary": "Поговорили."}
            if isinstance(item, Exception):
                raise item
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, item)], usage=usage)
        self.seen.append(Seen(info.model_settings and "", list(messages), info.instructions))
        item = self.replies.pop(0) if self.replies else f"ответ {len(self.seen)}"
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict) and "tool" in item:  # модель вызывает инструмент
            return ModelResponse(parts=[ToolCallPart(item["tool"], item.get("args", {}))], usage=usage)
        return ModelResponse(parts=[TextPart(item)],
                             usage=RequestUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens))


@pytest.fixture(autouse=True)
def _isolated_environment():
    """Ключи провайдеров из окружения не должны влиять на тесты, а движок, кладущий
    их в os.environ, — на следующие тесты."""
    saved = dict(os.environ)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_BASE_URL", "BOT_TOKEN",
                 "ALLOWED_TELEGRAM_IDS"):
        os.environ.pop(name, None)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_clock():
    CLOCK.now = NOW
    yield


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def settings(db_url) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:TEST",
        allowed_telegram_ids=[OWNER],
        openai_api_key="test-key",
        database_url=db_url,
    )


@pytest.fixture
async def sessions(db_url):
    await db.upgrade(db_url)
    engine = db.make_engine(db_url)
    yield db.make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def script() -> Script:
    return Script()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


AssistantFactory = Callable[..., Assistant]


@pytest.fixture
def make_assistant(sessions, settings, script, clock) -> AssistantFactory:
    def factory(*, settings_override: dict | None = None, script_override: Script | None = None,
                sessions_override=None) -> Assistant:
        s = settings.model_copy(update=settings_override or {})
        sc = script_override or script
        sess = sessions_override or sessions
        engine = PydanticAIEngine(
            s, model_factory=lambda m: FunctionModel(sc.fn, model_name=m.provider_model_name)
        )
        catalog = load_catalog(s.models_file)
        budget = BudgetGuard(sess, monthly_limit_usd=s.monthly_budget_usd,
                             disabled=s.paid_apis_disabled, attempts_per_call=s.llm_max_retries + 1)
        memory = MemoryEngine(sessions=sess, settings=s, catalog=catalog, engine=engine, budget=budget,
                              store=MemoryStore(sess, now=lambda: NOW)) if s.memory_enabled else None
        return Assistant(
            sessions=sess, settings=s, catalog=catalog, engine=engine, budget=budget, memory=memory,
            reminders=ReminderService(sess, now=lambda: CLOCK.now),
            rate_limiter=RateLimiter(s.messages_per_minute, clock=clock),
            breaker=CircuitBreaker(s.breaker_failure_threshold, s.breaker_cooldown_seconds, clock=clock),
            base_instructions=(PROJECT_ROOT / "config" / "assistant.md").read_text(encoding="utf-8"),
            now=lambda: CLOCK.now,
        )

    return factory


@pytest.fixture
def assistant(make_assistant) -> Assistant:
    return make_assistant()


def make_audio(seconds: float = 2.0, fmt: str = "ogg") -> bytes:
    """Синусоида заданной длины в mp3 или ogg/opus — тестовый «голос» без микрофона."""
    import io

    import av
    import numpy as np

    rate = 48_000 if fmt == "ogg" else 24_000
    codec = "libopus" if fmt == "ogg" else "mp3"
    buf = io.BytesIO()
    with av.open(buf, "w", format=fmt) as c:
        stream = c.add_stream(codec, rate=rate)
        stream.layout = "mono"
        t = np.arange(int(rate * seconds)) / rate
        pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).reshape(1, -1)
        frame_size = 960 if fmt == "ogg" else 1152
        for i in range(0, pcm.shape[1], frame_size):
            fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm[:, i:i + frame_size]), format="s16", layout="mono")
            fr.sample_rate = rate
            for p in stream.encode(fr):
                c.mux(p)
        for p in stream.encode(None):
            c.mux(p)
    return buf.getvalue()
