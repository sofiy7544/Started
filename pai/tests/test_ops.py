"""Резервные копии и самодиагностика."""

from __future__ import annotations

import sqlite3

from pai.ops import backup, doctor
from tests.conftest import OWNER


async def test_backup_includes_fresh_wal_writes_and_rotates(assistant, db_url, tmp_path) -> None:
    user = await assistant.get_user(OWNER)
    await assistant.memory.store.add(user.id, "fact", "Свежая запись в WAL")
    target_dir = tmp_path / "backups"
    paths = []
    for i in range(4):
        path = backup.backup_sqlite(db_url, target_dir, keep=2)
        (target_dir / f"pai-2000010{i}-000000.db").write_bytes(b"")  # старые копии для ротации
        paths.append(path)
    rows = sqlite3.connect(paths[-1]).execute("select content from memories").fetchall()
    assert rows == [("Свежая запись в WAL",)]  # копия через backup API, а не копирование файла
    assert len(list(target_dir.glob("pai-*.db"))) <= 3


def test_backup_skips_non_sqlite(tmp_path) -> None:
    assert backup.backup_sqlite("postgresql+asyncpg://u:p@h/db", tmp_path) is None


def test_doctor_never_prints_secrets(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:SUPER_SECRET_TOKEN")
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "111")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-very-secret")
    monkeypatch.setenv("PAI_ENV_FILE", "/nonexistent")
    text = "\n".join(doctor.report())
    assert "Оперативная память" in text and "OpenAI: задан" in text and "Настройки в порядке" in text
    assert "SUPER_SECRET" not in text and "sk-very-secret" not in text


async def test_migrations_match_models_and_keep_fts(sessions, db_url) -> None:
    """Регрессия: автогенерация Alembic предлагала удалить служебные таблицы поиска FTS5."""
    import importlib.util

    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from pai.db.engine import MIGRATIONS_DIR
    from pai.db.models import Base

    spec = importlib.util.spec_from_file_location("pai_env_include", MIGRATIONS_DIR / "env.py")
    source = (MIGRATIONS_DIR / "env.py").read_text(encoding="utf-8")
    include_src = "def _include_object" + source.split("def _include_object")[1].split("\ndef _run")[0]
    ns: dict = {}
    exec(include_src, ns)  # тот же фильтр, что использует env.py
    del spec
    engine = create_engine(db_url.replace("+aiosqlite", ""))
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={
            "include_object": ns["_include_object"], "compare_type": True}), Base.metadata)
        fts = sorted(r[0] for r in conn.exec_driver_sql("select name from sqlite_master where name like '%_fts'"))
    engine.dispose()
    assert diff == []
    assert fts == ["episodes_fts", "memories_fts"]
