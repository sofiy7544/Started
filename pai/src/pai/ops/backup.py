"""Резервные копии SQLite через встроенный backup API (а не копированием файла: в режиме WAL
свежие записи лежат в отдельном файле -wal, и простая копия их теряет)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
BACKUP_INTERVAL_SECONDS = 24 * 3600


def sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    path = database_url.split("///", 1)[-1]
    return None if not path or path == ":memory:" else Path(path)


def backup_sqlite(database_url: str, backup_dir: Path, keep: int = 14) -> Path | None:
    """Делает копию базы и удаляет старые сверх `keep`. None — база не SQLite (PostgreSQL
    копируется средствами сервера, см. docs)."""
    src_path = sqlite_path(database_url)
    if src_path is None or not src_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"pai-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob("pai-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return target


async def run_forever(database_url: str, backup_dir: Path, keep: int) -> None:
    while True:
        try:
            path = await asyncio.to_thread(backup_sqlite, database_url, backup_dir, keep)
            if path:
                log.info("резервная копия базы: %s", path.name)
        except (OSError, sqlite3.Error) as exc:
            log.warning("резервная копия не создана: %s", type(exc).__name__)
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
