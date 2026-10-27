"""Месячный бюджет на Claude API: учёт каждого вызова и аварийное отключение."""

from __future__ import annotations

import enum
import logging
from decimal import Decimal

import asyncpg

from companion import pricing
from companion.config import Settings
from companion.llm.client import LLMResponse
from companion.repositories import usage

log = logging.getLogger(__name__)
PROVIDER = "anthropic"


class BudgetLevel(enum.Enum):
    OK = "ok"
    SOFT = "soft"  # израсходовано больше BUDGET_SOFT_RATIO — переходим на дешёвую модель
    EXHAUSTED = "exhausted"  # платные вызовы запрещены до следующего месяца


class BudgetGuard:
    def __init__(self, pool: asyncpg.Pool, settings: Settings) -> None:
        self._pool = pool
        self._limit = Decimal(str(settings.anthropic_monthly_budget_usd))
        self._soft = self._limit * Decimal(str(settings.budget_soft_ratio))
        self._disabled = settings.paid_apis_disabled

    async def spent(self) -> Decimal:
        return await usage.spent_this_month(self._pool, PROVIDER)

    async def level(self) -> BudgetLevel:
        if self._disabled:
            return BudgetLevel.EXHAUSTED
        spent = await self.spent()
        if spent >= self._limit:
            return BudgetLevel.EXHAUSTED
        if spent >= self._soft:
            return BudgetLevel.SOFT
        return BudgetLevel.OK

    async def can_afford(self, model: str, *, input_chars: int, max_output_tokens: int) -> bool:
        """Проверка до вызова по верхней оценке (≈3 символа на токен для русского текста)."""
        if self._disabled:
            return False
        estimate = pricing.max_cost_usd(
            model, input_tokens_estimate=input_chars // 3 + 1, max_output_tokens=max_output_tokens
        )
        ok = await self.spent() + estimate <= self._limit
        if not ok:
            log.warning("budget: call blocked, monthly limit %s USD reached", self._limit)
        return ok

    async def record(self, resp: LLMResponse, *, operation: str, user_id: int | None) -> Decimal:
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
