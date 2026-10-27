"""Месячный бюджет на Claude API: резервирование до вызова, учёт после, аварийное отключение.

Учёт ведётся в USD за календарный месяц по UTC (сумма api_usage.cost_usd с 1-го числа).
Перед каждым вызовом резервируется верхняя оценка его стоимости; проверка «потрачено +
зарезервировано + оценка ≤ лимит» и резерв делаются атомарно, поэтому одновременные
запросы не могут вместе перешагнуть лимит. Резерв живёт в памяти процесса — бот
рассчитан на один экземпляр (так и работает long polling).
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

import asyncpg

from companion import pricing
from companion.config import Settings
from companion.llm.client import LLMError, LLMResponse
from companion.repositories import usage

log = logging.getLogger(__name__)
T = TypeVar("T")
PROVIDER = "anthropic"
# Грубо и с запасом: в русском тексте на токен приходится больше 2 символов.
CHARS_PER_TOKEN_ESTIMATE = 2


class BudgetLevel(enum.Enum):
    OK = "ok"
    SOFT = "soft"  # израсходовано больше BUDGET_SOFT_RATIO — переходим на дешёвую модель
    EXHAUSTED = "exhausted"  # платные вызовы запрещены до следующего месяца


@dataclass
class Reservation:
    model: str
    estimate: Decimal
    released: bool = False


class BudgetGuard:
    def __init__(self, pool: asyncpg.Pool, settings: Settings) -> None:
        self._pool = pool
        self._limit = Decimal(str(settings.anthropic_monthly_budget_usd))
        self._soft = self._limit * Decimal(str(settings.budget_soft_ratio))
        self._disabled = settings.paid_apis_disabled
        self._reserved = Decimal(0)
        self._lock = asyncio.Lock()

    @property
    def reserved(self) -> Decimal:
        return self._reserved

    async def spent(self) -> Decimal:
        return await usage.spent_this_month(self._pool, PROVIDER)

    async def level(self) -> BudgetLevel:
        if self._disabled:
            return BudgetLevel.EXHAUSTED
        spent = await self.spent() + self._reserved
        if spent >= self._limit:
            return BudgetLevel.EXHAUSTED
        if spent >= self._soft:
            return BudgetLevel.SOFT
        return BudgetLevel.OK

    async def reserve(
        self, model: str, *, input_chars: int, max_output_tokens: int
    ) -> Reservation | None:
        """Резервирует верхнюю оценку стоимости. None — вызов делать нельзя."""
        if self._disabled:
            return None
        estimate = pricing.max_cost_usd(
            model,
            input_tokens_estimate=input_chars // CHARS_PER_TOKEN_ESTIMATE + 1,
            max_output_tokens=max_output_tokens,
        )
        async with self._lock:
            if await self.spent() + self._reserved + estimate > self._limit:
                log.warning("budget: call blocked, monthly limit %s USD reached", self._limit)
                return None
            self._reserved += estimate
        return Reservation(model, estimate)

    def release(self, res: Reservation) -> None:
        """Снять резерв без записи расхода (запрос точно не тарифицирован)."""
        if not res.released:
            res.released = True
            self._reserved -= res.estimate

    async def settle(
        self, res: Reservation, resp: LLMResponse, *, operation: str, user_id: int | None
    ) -> Decimal:
        """Записать фактический расход по usage из ответа и снять резерв."""
        try:
            return await self._record(resp, operation=operation, user_id=user_id)
        finally:
            self.release(res)

    async def settle_unknown(
        self, res: Reservation, *, attempts: int, operation: str, user_id: int | None
    ) -> Decimal:
        """Исход неизвестен (таймаут, отмена): записываем верхнюю оценку за каждую попытку."""
        cost = res.estimate * max(attempts, 1)
        try:
            await usage.record(
                self._pool, provider=PROVIDER, model=res.model,
                operation=f"{operation}_unconfirmed", user_id=user_id, cost_usd=cost,
            )
            log.warning("llm usage unconfirmed: op=%s user=%s model=%s estimated cost=%s",
                        operation, user_id, res.model, cost)
        finally:
            self.release(res)
        return cost

    async def paid_call(
        self,
        res: Reservation,
        call: Callable[[], Awaitable[T]],
        response_of: Callable[[T], LLMResponse],
        *,
        operation: str,
        user_id: int | None,
    ) -> T:
        """Выполнить платный вызов под резервом и корректно учесть его при любом исходе:
        ответ — по фактическому usage; HTTP-ошибка — без расхода (ответа с usage нет);
        таймаут или отмена — верхняя оценка, т. к. генерация могла состояться."""
        try:
            result = await call()
        except LLMError as exc:
            if exc.possible_billed_attempts:
                await self.settle_unknown(
                    res, attempts=exc.possible_billed_attempts, operation=operation, user_id=user_id
                )
            else:
                self.release(res)
            raise
        except asyncio.CancelledError:
            await asyncio.shield(
                self.settle_unknown(res, attempts=1, operation=operation, user_id=user_id)
            )
            raise
        except BaseException:
            self.release(res)
            raise
        await asyncio.shield(self.settle(res, response_of(result), operation=operation, user_id=user_id))
        return result

    async def _record(self, resp: LLMResponse, *, operation: str, user_id: int | None) -> Decimal:
        # При серверном fallback ответ мог прийти от другой модели — считаем по более дорогой.
        cost = max(
            pricing.cost_usd(
                m,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cache_read_tokens=resp.usage.cache_read_tokens,
                cache_write_tokens=resp.usage.cache_write_tokens,
            )
            for m in {resp.requested_model, resp.served_model}
        )
        await usage.record(
            self._pool,
            provider=PROVIDER,
            model=resp.served_model,
            operation=operation,
            user_id=user_id,
            cost_usd=cost,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=resp.usage.cache_read_tokens,
            cache_write_tokens=resp.usage.cache_write_tokens,
        )
        log.info(
            "llm usage: op=%s user=%s model=%s in=%d out=%d cache_r=%d cache_w=%d cost=%s",
            operation, user_id, resp.served_model, resp.usage.input_tokens,
            resp.usage.output_tokens, resp.usage.cache_read_tokens,
            resp.usage.cache_write_tokens, cost,
        )
        return cost
