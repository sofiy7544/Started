from __future__ import annotations

from decimal import Decimal

import asyncpg

Conn = asyncpg.Pool | asyncpg.Connection


async def record(
    conn: Conn,
    *,
    provider: str,
    model: str,
    operation: str,
    user_id: int | None,
    cost_usd: Decimal,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    await conn.execute(
        # Деньги потрачены в любом случае: если пользователь успел удалить данные,
        # расход сохраняется без привязки к нему.
        """INSERT INTO api_usage (provider, model, operation, user_id, input_tokens,
               output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
           VALUES ($1, $2, $3, (SELECT id FROM users WHERE id = $4::bigint),
                   $5, $6, $7, $8, $9)""",
        provider,
        model,
        operation,
        user_id,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        cost_usd,
    )


async def spent_this_month(conn: Conn, provider: str) -> Decimal:
    value = await conn.fetchval(
        """SELECT COALESCE(sum(cost_usd), 0) FROM api_usage
           WHERE provider = $1
             AND created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        provider,
    )
    return Decimal(value)
