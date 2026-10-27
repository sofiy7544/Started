from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

Conn = asyncpg.Pool | asyncpg.Connection


@dataclass(frozen=True)
class User:
    id: int
    tg_user_id: int
    first_name: str | None
    age_confirmed_at: datetime | None
    context_from_message_id: int

    @property
    def age_confirmed(self) -> bool:
        return self.age_confirmed_at is not None


_COLUMNS = "id, tg_user_id, first_name, age_confirmed_at, context_from_message_id"


def _to_user(row: asyncpg.Record) -> User:
    return User(**{k: row[k] for k in _COLUMNS.split(", ")})


async def upsert(
    conn: Conn, tg_user_id: int, first_name: str | None, language_code: str | None
) -> User:
    row = await conn.fetchrow(
        f"""INSERT INTO users (tg_user_id, first_name, language_code)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_user_id) DO UPDATE
               SET first_name = EXCLUDED.first_name,
                   language_code = EXCLUDED.language_code,
                   last_active_at = now()
            RETURNING {_COLUMNS}""",
        tg_user_id,
        first_name,
        language_code,
    )
    return _to_user(row)


async def get(conn: Conn, user_id: int) -> User | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM users WHERE id = $1", user_id)
    return _to_user(row) if row else None


async def get_by_tg_id(conn: Conn, tg_user_id: int) -> User | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM users WHERE tg_user_id = $1", tg_user_id)
    return _to_user(row) if row else None


async def confirm_age(conn: Conn, user_id: int) -> None:
    await conn.execute(
        "UPDATE users SET age_confirmed_at = COALESCE(age_confirmed_at, now()) WHERE id = $1",
        user_id,
    )


async def reset_context(conn: Conn, user_id: int) -> None:
    """Новый диалог: прежние сообщения больше не попадают в контекст (но остаются для резюме)."""
    await conn.execute(
        """UPDATE users SET context_from_message_id =
               COALESCE((SELECT max(id) FROM messages WHERE user_id = $1), 0)
           WHERE id = $1""",
        user_id,
    )


async def delete(conn: Conn, user_id: int) -> None:
    """Удаляет пользователя; история, факты и резюме удаляются каскадно."""
    await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def mark_update_processed(conn: Conn, update_id: int) -> bool:
    """True, если апдейт новый; False, если уже обрабатывался."""
    inserted = await conn.fetchval(
        """INSERT INTO processed_updates (update_id) VALUES ($1)
           ON CONFLICT DO NOTHING RETURNING update_id""",
        update_id,
    )
    return inserted is not None


async def prune_processed_updates(conn: Conn, keep_days: int = 3) -> None:
    await conn.execute(
        "DELETE FROM processed_updates WHERE created_at < now() - make_interval(days => $1)",
        keep_days,
    )
