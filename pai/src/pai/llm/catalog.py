"""Каталог моделей из config/models.toml и расчёт стоимости вызовов."""

from __future__ import annotations

import tomllib
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from genai_prices import Usage as PriceUsage
from genai_prices import calc_price

from pai.config import ConfigError, Settings

PROVIDERS = ("openai", "anthropic", "ollama")
# Если цены модели нет ни в genai-prices, ни в каталоге, считаем по самому дорогому тарифу,
# чтобы бюджет ошибался в безопасную сторону.
FALLBACK_PRICE_IN = Decimal("15")
FALLBACK_PRICE_OUT = Decimal("75")
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    title: str
    note: str
    provider: str
    api_model: str
    price_in: Decimal | None = None  # ручная цена, USD за 1M входных токенов
    price_out: Decimal | None = None

    @property
    def provider_model_name(self) -> str:
        """Имя модели у провайдера: «openai-responses:gpt-6-luna» → «gpt-6-luna»."""
        return self.api_model.split(":", 1)[1]

    def cost_usd(
        self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """input_tokens — ВСЕ входные токены, включая кэшированные (так считают pydantic-ai
        и genai-prices); cache_* — сколько из них прочитано из кэша / записано в кэш."""
        if self.price_in is not None and self.price_out is not None:
            # Ручная цена без скидки за кэш — оценка с запасом.
            total = self.price_in * input_tokens + self.price_out * output_tokens
            return (total / _MILLION).quantize(Decimal("0.000001"))
        price_provider = "anthropic" if self.provider == "anthropic" else self.provider
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = calc_price(
                    PriceUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens or None,
                        cache_write_tokens=cache_write_tokens or None,
                    ),
                    model_ref=self.provider_model_name,
                    provider_id=price_provider,
                )
            return Decimal(str(result.total_price)).quantize(Decimal("0.000001"))
        except LookupError:
            total = FALLBACK_PRICE_IN * input_tokens + FALLBACK_PRICE_OUT * output_tokens
            return (total / _MILLION).quantize(Decimal("0.000001"))

    def price_known(self) -> bool:
        if self.price_in is not None and self.price_out is not None:
            return True
        try:
            calc_price(PriceUsage(input_tokens=1, output_tokens=1),
                       model_ref=self.provider_model_name, provider_id=self.provider)
            return True
        except LookupError:
            return False


@dataclass(frozen=True)
class Catalog:
    models: tuple[ModelInfo, ...]
    default_id: str

    def get(self, model_id: str | None) -> ModelInfo | None:
        return next((m for m in self.models if m.id == model_id), None)

    def available(self, settings: Settings) -> list[ModelInfo]:
        return [m for m in self.models if settings.provider_available(m.provider)]

    def resolve(self, model_id: str | None, settings: Settings) -> ModelInfo:
        """Выбранная модель, если она доступна; иначе модель по умолчанию; иначе первая доступная."""
        available = self.available(settings)
        if not available:
            raise ConfigError("ни одна модель из каталога не доступна — проверьте ключи провайдеров")
        for candidate in (model_id, self.default_id):
            found = next((m for m in available if m.id == candidate), None)
            if found:
                return found
        return available[0]


def load_catalog(path: Path) -> Catalog:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"не найден каталог моделей: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"ошибка в каталоге моделей {path}: {exc}") from exc

    models = []
    seen: set[str] = set()
    for raw in data.get("model", []):
        try:
            mid, provider, api_model = raw["id"], raw["provider"], raw["api_model"]
        except KeyError as exc:
            raise ConfigError(f"в каталоге моделей у модели нет поля {exc}") from exc
        if provider not in PROVIDERS:
            raise ConfigError(f"модель {mid}: неизвестный провайдер {provider!r}")
        if ":" not in api_model:
            raise ConfigError(f"модель {mid}: api_model должен быть вида 'провайдер:модель'")
        if mid in seen:
            raise ConfigError(f"модель {mid} указана в каталоге дважды")
        seen.add(mid)
        price_in = raw.get("price_in")
        price_out = raw.get("price_out")
        models.append(ModelInfo(
            id=mid, title=raw.get("title", mid), note=raw.get("note", ""), provider=provider,
            api_model=api_model,
            price_in=None if price_in is None else Decimal(str(price_in)),
            price_out=None if price_out is None else Decimal(str(price_out)),
        ))
    if not models:
        raise ConfigError("в каталоге моделей нет ни одной модели")
    default_id = data.get("default", models[0].id)
    if default_id not in seen:
        raise ConfigError(f"модель по умолчанию {default_id!r} не описана в каталоге")
    return Catalog(tuple(models), default_id)
