"""Подключение к базе и применение миграций при старте."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        # Папку для файла базы создаём сами — пользователю не нужно ничего готовить.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")  # каскадное удаление данных пользователя
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


async def upgrade(url: str) -> None:
    """Применяет недостающие миграции (идемпотентно)."""
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")


async def current_revision(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
    finally:
        await engine.dispose()
