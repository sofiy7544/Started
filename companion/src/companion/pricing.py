"""Цены моделей (USD за 1M токенов) и расчёт стоимости вызова.

Источник: https://platform.claude.com/docs/en/about-claude/pricing (проверено 2026-10-27).
Цены — Claude API первой стороны, глобальная маршрутизация (без inference_geo).
Неизвестная модель считается по самому дорогому тарифу, чтобы бюджет ошибался
в безопасную сторону.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # запись в кэш с TTL 5 минут (используется по умолчанию)
    # Тариф для длинных запросов (Haiku 5.5: промпт больше 100 000 токенов, включая кэш).
    long_context_threshold: int | None = None
    long_context: ModelPrice | None = None


def _p(inp: str, cache_write: str, cache_read: str, out: str, **kw: object) -> ModelPrice:
    return ModelPrice(Decimal(inp), Decimal(out), Decimal(cache_read), Decimal(cache_write), **kw)  # type: ignore[arg-type]


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": _p(
        "0.10", "0.125", "0.01", "0.50",
        long_context_threshold=100_000,
        long_context=_p("0.50", "0.625", "0.05", "2.50"),
    ),
    "claude-sonnet-5-5": _p("2.00", "2.50", "0.10", "10.00"),
    "claude-opus-5-5": _p("4.00", "5.00", "0.20", "20.00"),
}
FALLBACK_PRICE = _p("10.00", "12.50", "1.00", "50.00")

_MILLION = Decimal(1_000_000)


def is_known(model: str) -> bool:
    return model in PRICES


def price_for(model: str, prompt_tokens: int = 0) -> ModelPrice:
    p = PRICES.get(model, FALLBACK_PRICE)
    if p.long_context and p.long_context_threshold and prompt_tokens > p.long_context_threshold:
        return p.long_context
    return p


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    p = price_for(model, prompt)
    total = (
        p.input * input_tokens
        + p.output * output_tokens
        + p.cache_read * cache_read_tokens
        + p.cache_write * cache_write_tokens
    )
    return (total / _MILLION).quantize(Decimal("0.000001"))


def max_cost_usd(model: str, *, input_tokens_estimate: int, max_output_tokens: int) -> Decimal:
    """Верхняя оценка стоимости вызова: весь вход как запись в кэш, весь лимит ответа."""
    return cost_usd(
        model, input_tokens=0, cache_write_tokens=input_tokens_estimate, output_tokens=max_output_tokens
    )
