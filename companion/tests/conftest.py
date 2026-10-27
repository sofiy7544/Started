from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime

import asyncpg
import pytest

from companion import db
from companion.config import PROJECT_ROOT, Settings
from companion.persona import Persona, load_persona
from companion.repositories import users
from companion.repositories.users import User
from companion.services.budget import BudgetGuard
from companion.services.chat import ChatService
from companion.services.limits import CircuitBreaker, RateLimiter
from companion.services.memory import MemoryService
from tests.fakes import FakeLLM

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://companion:companion@localhost:5432/companion_test"
)
TABLES = "users, messages, conversation_summaries, memory_facts, api_usage, processed_updates"


@pytest.fixture(scope="session", autouse=True)
def _fresh_schema() -> None:
    """Один раз за прогон: пустая схема и миграции с нуля (проверяет и сами миграции)."""

    async def setup() -> None:
        try:
            conn = await asyncpg.connect(TEST_DB, timeout=5)
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.exit(f"PostgreSQL для тестов недоступен ({TEST_DB}): {exc}", returncode=3)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
        pool = await db.create_pool(TEST_DB)
        await db.apply_migrations(pool)
        await pool.close()

    asyncio.run(setup())


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await db.create_pool(TEST_DB)
    await p.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="123456:TEST",
        database_url=TEST_DB,
        keep_recent_messages=4,
        summarize_threshold=8,
        max_context_messages=10,
        messages_per_minute=1000,
        daily_messages_per_user=1000,
    )


@pytest.fixture(scope="session")
def persona() -> Persona:
    return load_persona(PROJECT_ROOT / "persona" / "mira.toml")


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


ServiceFactory = Callable[..., ChatService]


@pytest.fixture
def make_service(
    pool: asyncpg.Pool, settings: Settings, persona: Persona, llm: FakeLLM, clock: Clock
) -> ServiceFactory:
    def factory(
        *, settings_override: dict | None = None, llm_override: FakeLLM | None = None,
        pool_override: asyncpg.Pool | None = None,
    ) -> ChatService:
        s = replace(settings, **(settings_override or {}))
        p = pool_override or pool
        model = llm_override or llm
        budget = BudgetGuard(p, s)
        return ChatService(
            pool=p,
            llm=model,
            settings=s,
            persona=persona,
            budget=budget,
            memory_service=MemoryService(p, model, budget, s, persona.name),
            rate_limiter=RateLimiter(s.messages_per_minute, clock=clock),
            breaker=CircuitBreaker(s.breaker_failure_threshold, s.breaker_cooldown_seconds, clock=clock),
            now=lambda: datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
        )

    return factory


@pytest.fixture
def service(make_service: ServiceFactory) -> ChatService:
    return make_service()


async def new_user(pool: asyncpg.Pool, tg_id: int, name: str = "Тест", confirmed: bool = True) -> User:
    user = await users.upsert(pool, tg_id, name, "ru")
    if confirmed:
        await users.confirm_age(pool, user.id)
        user = await users.get(pool, user.id)
    return user
