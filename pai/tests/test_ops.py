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
