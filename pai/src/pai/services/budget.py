"""Месячный бюджет: резерв до вызова, учёт после, аварийное отключение.

Учёт в USD за календарный месяц по UTC. Перед вызовом резервируется верхняя оценка
стоимости; проверка «потрачено + зарезервировано + оценка ≤ лимит» и резерв выполняются
под одной блокировкой, поэтому параллельные запросы не могут вместе превысить лимит.
Исход неизвестен (таймаут, отмена) — записывается верхняя оценка. Ошибка API с ответом
провайдера — расход не записывается (ответа с usage нет). Резерв хранится в памяти
процесса: бот рассчитан на один экземпляр (так работает long polling).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import UsageRecord
from pai.llm.catalog import ModelInfo
from pai.llm.engine import LLMFailure, Reply

log = logging.getLogger(__name__)
T = TypeVar("T")
CHARS_PER_TOKEN_ESTIMATE = 2  # с запасом: в русском тексте на токен приходится больше символов


def month_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Reservation:
    model: ModelInfo
    estimate: Decimal
    released: bool = False


class BudgetGuard:
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        monthly_limit_usd: float,
        disabled: bool = False,
        attempts_per_call: int = 1,
    ) -> None:
        self._sessions = sessions
        # При таймауте SDK повторяет запрос: каждая попытка могла быть оплачена.
        self._attempts = max(1, attempts_per_call)
        self._limit = Decimal(str(monthly_limit_usd))
        self._disabled = disabled
        self._reserved = Decimal(0)
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> Decimal:
        return self._limit

    @property
    def reserved(self) -> Decimal:
        return self._reserved

    @property
    def disabled(self) -> bool:
        return self._disabled

    async def spent_this_month(self) -> Decimal:
        async with self._sessions() as s:
            value = await s.scalar(
                select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                    UsageRecord.created_at >= month_start()
                )
            )
        return Decimal(str(value))

    async def remaining(self) -> Decimal:
        return max(Decimal(0), self._limit - await self.spent_this_month() - self._reserved)

    def estimate(self, model: ModelInfo, *, input_chars: int, max_output_tokens: int) -> Decimal:
        return model.cost_usd(
            input_tokens=input_chars // CHARS_PER_TOKEN_ESTIMATE + 1, output_tokens=max_output_tokens
        )

    async def reserve(
        self, model: ModelInfo, *, input_chars: int, max_output_tokens: int
    ) -> Reservation | None:
        """Резервирует верхнюю оценку стоимости. None — вызов делать нельзя."""
        estimate = self.estimate(model, input_chars=input_chars, max_output_tokens=max_output_tokens)
        if self._disabled and estimate > 0:
            return None
        async with self._lock:
            if estimate > 0 and await self.spent_this_month() + self._reserved + estimate > self._limit:
                log.warning("budget: call blocked, monthly limit %s USD reached", self._limit)
                return None
            self._reserved += estimate
        return Reservation(model, estimate)

    def release(self, res: Reservation) -> None:
        if not res.released:
            res.released = True
            self._reserved -= res.estimate

    async def _record(
        self,
        res: Reservation,
        *,
        user_id: int | None,
        operation: str,
        cost: Decimal,
        confirmed: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                UsageRecord(
                    user_id=user_id,
                    provider=res.model.provider,
                    model_id=res.model.id,
                    operation=operation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cost_usd=cost,
                    confirmed=confirmed,
                )
            )
        log.info(
            "llm usage: op=%s user=%s model=%s in=%d out=%d cache_r=%d cost=%s confirmed=%s",
            operation,
            user_id,
            res.model.id,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cost,
            confirmed,
        )

    async def paid_call(
        self,
        res: Reservation,
        call: Callable[[], Awaitable[T]],
        *,
        user_id: int | None,
        operation: str,
        response_of: Callable[[T], Reply] = lambda r: r,  # type: ignore[assignment,return-value]
    ) -> T:
        """Выполнить платный вызов под резервом и учесть его при любом исходе.
        response_of достаёт Reply (usage, стоимость) из результата вызова."""
        try:
            result = await call()
        except LLMFailure as exc:
            if exc.billed is not None:
                await asyncio.shield(self._settle(res, exc.billed, user_id=user_id, operation=operation))
            elif exc.possibly_billed:
                await asyncio.shield(self._settle_unknown(res, user_id=user_id, operation=operation))
            else:
                self.release(res)
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._settle_unknown(res, user_id=user_id, operation=operation))
            raise
        except BaseException:
            self.release(res)
            raise
        await asyncio.shield(self._settle(res, response_of(result), user_id=user_id, operation=operation))
        return result

    async def _settle(self, res: Reservation, reply: Reply, *, user_id: int | None, operation: str) -> None:
        try:
            await self._record(
                res,
                user_id=user_id,
                operation=operation,
                cost=reply.cost_usd,
                confirmed=True,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                cache_read_tokens=reply.cache_read_tokens,
            )
        finally:
            self.release(res)

    async def _settle_unknown(self, res: Reservation, *, user_id: int | None, operation: str) -> None:
        try:
            await self._record(
                res, user_id=user_id, operation=operation, cost=res.estimate * self._attempts, confirmed=False
            )
        finally:
            self.release(res)
