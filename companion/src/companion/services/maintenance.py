"""Плановое обслуживание: срок хранения переписки и очистка служебных записей."""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from companion.repositories import memory, users

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 6 * 3600


async def run_once(pool: asyncpg.Pool, retention_days: int) -> int:
    """Удаляет сообщения старше срока хранения у всех пользователей. Резюме и факты остаются
    до /forget. Возвращает число удалённых сообщений."""
    deleted = await memory.delete_expired_messages(pool, retention_days)
    await users.prune_processed_updates(pool)
    if deleted:
        log.info("retention: deleted %d messages older than %d days", deleted, retention_days)
    return deleted


async def run_forever(pool: asyncpg.Pool, retention_days: int) -> None:
    while True:
        try:
            await run_once(pool, retention_days)
        except (OSError, asyncpg.PostgresError):
            log.warning("retention job failed, will retry", exc_info=True)
        await asyncio.sleep(INTERVAL_SECONDS)
