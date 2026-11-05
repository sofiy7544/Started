"""Окружение Alembic: асинхронный движок, URL берётся из конфигурации приложения."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from pai.db.models import Base

config = context.config
target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Таблицы полнотекстового поиска (memories_fts*, episodes_fts*) создаются миграцией 0002
    вручную и не описаны в моделях — автогенерация не должна предлагать их удалить."""
    return not (type_ == "table" and name and "_fts" in name)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # ALTER TABLE в SQLite
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async())
