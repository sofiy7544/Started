"""История, резюме и факты. Каждая функция работает строго в пределах одного user_id."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

Conn = asyncpg.Pool | asyncpg.Connection

FACT_CATEGORIES = ("name", "interest", "preference", "fact", "thread")

# Пространство имён advisory-блокировок памяти пользователя (второй ключ — users.id).
# Её держит суммаризация на время вызова модели; очистка и удаление ждут её,
# иначе резюме и факты удалённой переписки записались бы уже после удаления.
_MEMORY_LOCK_NS = 7_041


async def try_lock_memory(conn: asyncpg.Connection, user_id: int) -> bool:
    """Неблокирующая попытка (внутри транзакции): False — память уже обрабатывается."""
    return await conn.fetchval(
        "SELECT pg_try_advisory_xact_lock($1, $2::int)", _MEMORY_LOCK_NS, user_id
    )


async def lock_memory(conn: asyncpg.Connection, user_id: int) -> None:
    """Дождаться окончания текущей обработки памяти пользователя (внутри транзакции)."""
    await conn.execute("SELECT pg_advisory_xact_lock($1, $2::int)", _MEMORY_LOCK_NS, user_id)


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class Fact:
    id: int
    category: str
    text: str


def _msg(r: asyncpg.Record) -> StoredMessage:
    return StoredMessage(r["id"], r["role"], r["content"], r["created_at"])


# Ответ встаёт сразу за сообщением, на которое отвечает, даже если пользователь
# успел написать ещё что-то, пока ответ генерировался.
_DIALOG_ORDER = "ORDER BY COALESCE(reply_to_id, id), id"


async def add_message(
    conn: Conn,
    user_id: int,
    role: str,
    content: str,
    model: str | None = None,
    reply_to_id: int | None = None,
) -> int:
    return await conn.fetchval(
        """INSERT INTO messages (user_id, role, content, model, reply_to_id)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        user_id,
        role,
        content,
        model,
        reply_to_id,
    )


async def latest_user_message_id(conn: Conn, user_id: int) -> int | None:
    return await conn.fetchval(
        "SELECT max(id) FROM messages WHERE user_id = $1 AND role = 'user'", user_id
    )


async def context_messages(
    conn: Conn, user_id: int, after_id: int, limit: int
) -> list[StoredMessage]:
    """Несжатые сообщения текущего диалога (после границы /reset), не больше limit последних."""
    rows = await conn.fetch(
        f"""SELECT id, role, content, created_at FROM (
               SELECT id, role, content, created_at, reply_to_id FROM messages
               WHERE user_id = $1 AND id > $2 AND NOT summarized
               ORDER BY id DESC LIMIT $3
           ) t {_DIALOG_ORDER}""",
        user_id,
        after_id,
        limit,
    )
    return [_msg(r) for r in rows]


async def last_message_before(conn: Conn, user_id: int, before_id: int) -> StoredMessage | None:
    row = await conn.fetchrow(
        """SELECT id, role, content, created_at FROM messages
           WHERE user_id = $1 AND id < $2 ORDER BY id DESC LIMIT 1""",
        user_id,
        before_id,
    )
    return _msg(row) if row else None


async def count_user_messages_today(conn: Conn, user_id: int) -> int:
    return await conn.fetchval(
        """SELECT count(*) FROM messages
           WHERE user_id = $1 AND role = 'user'
             AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        user_id,
    )


async def unsummarized_messages(conn: Conn, user_id: int) -> list[StoredMessage]:
    rows = await conn.fetch(
        f"""SELECT id, role, content, created_at FROM messages
           WHERE user_id = $1 AND NOT summarized {_DIALOG_ORDER}""",
        user_id,
    )
    return [_msg(r) for r in rows]


async def mark_summarized(conn: Conn, user_id: int, up_to_message_id: int) -> None:
    await conn.execute(
        "UPDATE messages SET summarized = TRUE WHERE user_id = $1 AND id <= $2 AND NOT summarized",
        user_id,
        up_to_message_id,
    )


async def delete_old_summarized(conn: Conn, user_id: int, retention_days: int) -> int:
    result = await conn.execute(
        """DELETE FROM messages
           WHERE user_id = $1 AND summarized
             AND created_at < now() - make_interval(days => $2)""",
        user_id,
        retention_days,
    )
    return int(result.split()[-1])


async def delete_expired_messages(conn: Conn, retention_days: int) -> int:
    """Срок хранения: сообщения старше retention_days удаляются у всех пользователей."""
    result = await conn.execute(
        "DELETE FROM messages WHERE created_at < now() - make_interval(days => $1)",
        retention_days,
    )
    return int(result.split()[-1])


async def add_summary(
    conn: Conn, user_id: int, content: str, first_message_id: int, last_message_id: int
) -> None:
    await conn.execute(
        """INSERT INTO conversation_summaries (user_id, content, first_message_id, last_message_id)
           VALUES ($1, $2, $3, $4)""",
        user_id,
        content,
        first_message_id,
        last_message_id,
    )


async def recent_summaries(conn: Conn, user_id: int, limit: int) -> list[str]:
    rows = await conn.fetch(
        """SELECT content FROM (
               SELECT id, content FROM conversation_summaries
               WHERE user_id = $1 ORDER BY id DESC LIMIT $2
           ) t ORDER BY id""",
        user_id,
        limit,
    )
    return [r["content"] for r in rows]


async def count_summaries(conn: Conn, user_id: int) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM conversation_summaries WHERE user_id = $1", user_id
    )


def normalize_fact(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split()).strip(" .!")


async def list_facts(conn: Conn, user_id: int) -> list[Fact]:
    rows = await conn.fetch(
        "SELECT id, category, fact FROM memory_facts WHERE user_id = $1 ORDER BY id", user_id
    )
    return [Fact(r["id"], r["category"], r["fact"]) for r in rows]


async def remove_facts(conn: Conn, user_id: int, fact_ids: list[int]) -> int:
    """Удаляет факты по id. Чужие id игнорируются благодаря условию на user_id."""
    if not fact_ids:
        return 0
    result = await conn.execute(
        "DELETE FROM memory_facts WHERE user_id = $1 AND id = ANY($2::bigint[])",
        user_id,
        fact_ids,
    )
    return int(result.split()[-1])


async def set_preferred_name(conn: Conn, user_id: int, name: str) -> None:
    """Обращение к пользователю хранится в единственном экземпляре: новое заменяет старое."""
    await conn.execute(
        "DELETE FROM memory_facts WHERE user_id = $1 AND category = 'name'", user_id
    )
    await add_facts(conn, user_id, [("name", name)], max_facts=10_000, max_threads=10_000)


async def add_facts(
    conn: Conn,
    user_id: int,
    facts: list[tuple[str, str]],
    *,
    max_facts: int,
    max_threads: int,
) -> None:
    for category, text in facts:
        await conn.execute(
            """INSERT INTO memory_facts (user_id, category, fact, fact_norm)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, fact_norm)
               DO UPDATE SET updated_at = now(), category = EXCLUDED.category""",
            user_id,
            category,
            text,
            normalize_fact(text),
        )
    # Вытесняем давно не подтверждавшиеся записи сверх лимитов.
    await conn.execute(
        """DELETE FROM memory_facts WHERE user_id = $1 AND category = 'thread' AND id IN (
               SELECT id FROM memory_facts WHERE user_id = $1 AND category = 'thread'
               ORDER BY updated_at DESC, id DESC OFFSET $2)""",
        user_id,
        max_threads,
    )
    await conn.execute(
        """DELETE FROM memory_facts WHERE user_id = $1 AND category <> 'name' AND id IN (
               SELECT id FROM memory_facts WHERE user_id = $1 AND category <> 'name'
               ORDER BY updated_at DESC, id DESC OFFSET $2)""",
        user_id,
        max_facts,
    )


async def clear_memory(conn: Conn, user_id: int) -> None:
    """Удаляет факты, резюме и историю переписки пользователя; сам аккаунт остаётся."""
    await conn.execute("DELETE FROM memory_facts WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM conversation_summaries WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM messages WHERE user_id = $1", user_id)
