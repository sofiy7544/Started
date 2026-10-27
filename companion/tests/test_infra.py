"""Миграции, срок хранения данных, Docker-артефакты."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion import db
from companion.config import PROJECT_ROOT
from companion.repositories import memory
from companion.services import maintenance
from tests.conftest import new_user


async def test_migrations_are_idempotent_and_ordered(pool) -> None:
    assert await db.apply_migrations(pool) == []  # повторный запуск ничего не меняет
    names = [r["name"] for r in await pool.fetch("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == sorted(p.name for p in (PROJECT_ROOT / "migrations").glob("*.sql"))


async def test_retention_deletes_old_messages_of_inactive_users(pool) -> None:
    active = await new_user(pool, 1)
    inactive = await new_user(pool, 2)
    for u in (active, inactive):
        await memory.add_message(pool, u.id, "user", "свежее")
        await memory.add_facts(pool, u.id, [("interest", "Любит джаз")], max_facts=60, max_threads=5)
        await memory.add_summary(pool, u.id, "резюме", 1, 1)
    old = datetime.now(UTC) - timedelta(days=91)
    await pool.execute(
        "INSERT INTO messages (user_id, role, content, created_at) VALUES ($1, 'user', 'старое', $2)",
        inactive.id, old,
    )
    assert await maintenance.run_once(pool, retention_days=90) == 1
    contents = [r["content"] for r in await pool.fetch("SELECT content FROM messages")]
    assert "старое" not in contents and contents.count("свежее") == 2
    # резюме и факты живут до /forget — так и написано в /privacy
    assert len(await memory.list_facts(pool, inactive.id)) == 1
    assert await memory.count_summaries(pool, inactive.id) == 1


def test_docker_context_excludes_secrets_and_tests() -> None:
    ignore = (PROJECT_ROOT / ".dockerignore").read_text()
    for entry in (".env", "tests/", "backups/", "logs/"):
        assert entry in ignore
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    assert "COPY . " not in dockerfile  # копируются только нужные каталоги
    assert "USER app" in dockerfile
    assert "ENV BOT_TOKEN" not in dockerfile and "ANTHROPIC_API_KEY" not in dockerfile


def test_env_example_has_no_values_for_secrets_and_no_inline_comments() -> None:
    for line in (PROJECT_ROOT / ".env.example").read_text().splitlines():
        if line.startswith(("BOT_TOKEN=", "ANTHROPIC_API_KEY=", "POSTGRES_PASSWORD=")):
            assert line.split("=", 1)[1] == ""
        if "=" in line and not line.lstrip().startswith("#"):
            assert "#" not in line  # docker compose может не отрезать комментарий после значения
        if line.lstrip().startswith("# ") and "=" in line and " #" in line.split("=", 1)[1]:
            raise AssertionError(f"комментарий после значения: {line}")
