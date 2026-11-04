"""Проверка ключей и моделей при старте — через бесплатные Models API, без расхода токенов."""

from __future__ import annotations

import logging
from dataclasses import replace

import anthropic
import openai

from pai.config import ConfigError, Settings
from pai.llm.catalog import Catalog

log = logging.getLogger(__name__)


async def _openai_models(settings: Settings) -> set[str]:
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=1, timeout=20)
    try:
        return {m.id async for m in client.models.list()}
    finally:
        await client.close()


async def _anthropic_models(settings: Settings) -> set[str]:
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), max_retries=1, timeout=20
    )
    try:
        return {m.id async for m in client.models.list()}
    finally:
        await client.close()


async def verify_catalog(catalog: Catalog, settings: Settings) -> Catalog:
    """Возвращает каталог только с теми моделями, которые реально доступны по ключам.
    Неверный ключ — понятная ConfigError. Сеть недоступна — предупреждение, каталог как есть."""
    known: dict[str, set[str] | None] = {}
    fetchers = {"openai": _openai_models, "anthropic": _anthropic_models}
    for provider, fetch in fetchers.items():
        if not settings.provider_available(provider):
            continue
        try:
            known[provider] = await fetch(settings)
        except (openai.AuthenticationError, anthropic.AuthenticationError) as exc:
            name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
            raise ConfigError(f"{provider} отклонил {name} — проверьте ключ") from exc
        except (openai.APIError, anthropic.APIError) as exc:
            log.warning("не удалось проверить модели %s при старте (%s) — проверка пропущена",
                        provider, type(exc).__name__)
            known[provider] = None

    kept = []
    for model in catalog.models:
        ids = known.get(model.provider)
        if ids is not None and model.provider_model_name not in ids:
            log.warning("модель %s (%s) недоступна для этого ключа — скрыта из меню",
                        model.id, model.provider_model_name)
            continue
        if not model.price_known():
            log.warning("цена модели %s неизвестна — расходы считаются по максимальному тарифу", model.id)
        kept.append(model)
    result = replace(catalog, models=tuple(kept))
    if not result.available(settings):
        raise ConfigError("ни одна модель из каталога не доступна по вашим ключам")
    return result
