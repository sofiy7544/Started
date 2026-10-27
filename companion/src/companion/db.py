"""Пул соединений и применение SQL-миграций."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

from companion.config import PROJECT_ROOT

log = logging.getLogger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
# Произвольная константа: один мигратор одновременно, даже при нескольких процессах.
_MIGRATION_LOCK_KEY = 7_041_998


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30)


async def apply_migrations(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Применяет ещё не применённые *.sql по порядку имён. Возвращает список применённых."""
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       name TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
                    )
                log.info("migration applied: %s", path.name)
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
    return applied
