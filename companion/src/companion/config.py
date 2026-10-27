"""Настройки приложения. Все секреты и лимиты — только из переменных окружения."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Не хватает обязательной переменной окружения или значение некорректно."""


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    raise ValueError(value)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    # Свой Bot API сервер (https://github.com/tdlib/telegram-bot-api) или фейковый в тестах.
    telegram_api_url: str | None = None

    # Модели: дешёвая Haiku для переписки и для фоновой работы с памятью.
    chat_model: str = "claude-haiku-5-5"
    memory_model: str = "claude-haiku-5-5"
    llm_effort: str = "low"
    llm_refusal_fallbacks: bool = True
    llm_max_retries: int = 2  # повторы SDK для 429/5xx/обрывов сети; больше не делаем
    llm_timeout_seconds: float = 45.0

    # Размеры запросов.
    max_reply_tokens: int = 1000
    max_user_message_chars: int = 2000
    max_context_chars: int = 16000
    max_context_messages: int = 40
    keep_recent_messages: int = 14
    summarize_threshold: int = 30
    max_facts_per_user: int = 60
    max_open_threads: int = 5
    message_retention_days: int = 90

    # Деньги и частота.
    anthropic_monthly_budget_usd: float = 20.0
    budget_soft_ratio: float = 0.8
    paid_apis_disabled: bool = False
    daily_messages_per_user: int = 80
    messages_per_minute: int = 8
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 60.0

    support_contact: str = ""
    persona_path: Path = PROJECT_ROOT / "persona" / "mira.toml"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [k for k in ("BOT_TOKEN", "DATABASE_URL") if not env.get(k)]
        if missing:
            raise ConfigError("Не заданы переменные окружения: " + ", ".join(missing))

        kwargs: dict[str, Any] = {"bot_token": env["BOT_TOKEN"], "database_url": env["DATABASE_URL"]}
        for f in _ENV_FIELDS:
            raw = env.get(f.upper())
            if raw is None or raw.strip() == "":
                continue
            parse = _parser_for(_DEFAULTS[f])
            try:
                kwargs[f] = parse(raw.strip())
            except ValueError as exc:
                raise ConfigError(f"{f.upper()}: некорректное значение {raw!r}") from exc

        settings = cls(**kwargs)
        settings._validate()
        return settings

    def _validate(self) -> None:
        if not self.keep_recent_messages < self.summarize_threshold <= self.max_context_messages:
            raise ConfigError(
                "Нужно KEEP_RECENT_MESSAGES < SUMMARIZE_THRESHOLD <= MAX_CONTEXT_MESSAGES"
            )
        if not 0 < self.budget_soft_ratio <= 1:
            raise ConfigError("BUDGET_SOFT_RATIO должен быть в диапазоне (0, 1]")
        positive = ("max_reply_tokens", "max_user_message_chars", "max_context_chars",
                    "daily_messages_per_user", "messages_per_minute", "breaker_failure_threshold")
        for name in positive:
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name.upper()} должен быть больше нуля")
        if self.llm_max_retries < 0 or self.llm_max_retries > 5:
            raise ConfigError("LLM_MAX_RETRIES должен быть от 0 до 5")
        if self.anthropic_monthly_budget_usd < 0:
            raise ConfigError("ANTHROPIC_MONTHLY_BUDGET_USD не может быть отрицательным")


def _parser_for(default: object) -> Callable[[str], Any]:
    # bool проверяется раньше int: bool — подкласс int.
    for typ, parse in ((bool, _bool), (int, int), (float, float), (Path, Path)):
        if isinstance(default, typ):
            return parse
    return str

# Тип значения по умолчанию определяет парсер; None (telegram_api_url) читается как строка.
_DEFAULTS = {
    name: f.default for name, f in Settings.__dataclass_fields__.items() if f.default is not MISSING
}
_ENV_FIELDS = list(_DEFAULTS)
