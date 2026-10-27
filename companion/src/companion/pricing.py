"""Цены моделей (USD за 1M токенов) и расчёт стоимости вызова.

Цены — первая сторона Anthropic API. Неизвестная модель считается по самому
дорогому тарифу, чтобы бюджет ошибался в безопасную сторону.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # запись в кэш с TTL 5 минут = 1.25 × input


def _price(inp: str, out: str, cache_read: str) -> ModelPrice:
    i = Decimal(inp)
    return ModelPrice(i, Decimal(out), Decimal(cache_read), i * Decimal("1.25"))


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": _price("0.10", "0.50", "0.01"),
    "claude-sonnet-5-5": _price("2.00", "10.00", "0.20"),
    "claude-opus-5-5": _price("4.00", "20.00", "0.20"),
}
FALLBACK_PRICE = _price("10.00", "50.00", "1.00")

_MILLION = Decimal(1_000_000)


def price_for(model: str) -> ModelPrice:
    return PRICES.get(model, FALLBACK_PRICE)


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    p = price_for(model)
    total = (
        p.input * input_tokens
        + p.output * output_tokens
        + p.cache_read * cache_read_tokens
        + p.cache_write * cache_write_tokens
    )
    return (total / _MILLION).quantize(Decimal("0.000001"))


def max_cost_usd(model: str, *, input_tokens_estimate: int, max_output_tokens: int) -> Decimal:
    """Верхняя оценка стоимости вызова — для проверки бюджета до запроса."""
    return cost_usd(model, input_tokens=input_tokens_estimate, output_tokens=max_output_tokens)
