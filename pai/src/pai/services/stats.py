"""Статистика использования для экрана «📊 Статистика»."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import Conversation, Turn, UsageRecord
from pai.services.budget import month_start


@dataclass(frozen=True)
class ModelLine:
    model_id: str
    requests: int
    cost_usd: Decimal


@dataclass(frozen=True)
class Stats:
    month_cost: Decimal
    today_cost: Decimal
    month_requests: int
    unconfirmed_cost: Decimal
    by_model: list[ModelLine]
    turns_total: int
    conversations_total: int


async def collect(sessions: async_sessionmaker, user_id: int, now: datetime | None = None) -> Stats:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = month_start(now)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with sessions() as s:
        rows = (await s.execute(
            select(UsageRecord.model_id, func.count(), func.coalesce(func.sum(UsageRecord.cost_usd), 0))
            .where(UsageRecord.created_at >= start)
            .group_by(UsageRecord.model_id)
            .order_by(func.sum(UsageRecord.cost_usd).desc())
        )).all()
        today = await s.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(UsageRecord.created_at >= day)
        )
        unconfirmed = await s.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                UsageRecord.created_at >= start, UsageRecord.confirmed.is_(False)
            )
        )
        turns = await s.scalar(select(func.count()).select_from(Turn).where(Turn.user_id == user_id))
        convs = await s.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        )
    by_model = [ModelLine(m, n, Decimal(str(c))) for m, n, c in rows]
    return Stats(
        month_cost=sum((m.cost_usd for m in by_model), Decimal(0)),
        today_cost=Decimal(str(today)),
        month_requests=sum(m.requests for m in by_model),
        unconfirmed_cost=Decimal(str(unconfirmed)),
        by_model=by_model,
        turns_total=turns or 0,
        conversations_total=convs or 0,
    )


def days_left_in_month(now: datetime | None = None) -> int:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt.date() - now.date()).days
