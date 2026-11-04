"""Настройки из переменных окружения и файла .env. Секреты в коде не хранятся."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Понятная ошибка конфигурации — показывается пользователю без трассировки."""


class Settings(BaseSettings):
    # Файл .env рядом с проектом; PAI_ENV_FILE позволяет указать другой (тестам — пустой).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("PAI_ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8", extra="ignore",
    )

    # --- обязательные ---
    bot_token: SecretStr
    # Белый список: только эти Telegram ID могут пользоваться ботом.
    allowed_telegram_ids: Annotated[list[int], NoDecode]

    # --- провайдеры моделей (нужен хотя бы один) ---
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    ollama_base_url: str | None = None  # например http://localhost:11434/v1

    # --- хранилище и файлы конфигурации ---
    database_url: str = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'pai.db').as_posix()}"
    models_file: Path = PROJECT_ROOT / "config" / "models.toml"
    instructions_file: Path = PROJECT_ROOT / "config" / "assistant.md"
    telegram_api_url: str | None = None  # свой Bot API сервер (обычно не нужен)

    # --- расходы ---
    monthly_budget_usd: float = Field(10.0, ge=0)
    per_request_cost_limit_usd: float = Field(0.25, gt=0)
    paid_apis_disabled: bool = False

    # --- размеры ---
    max_reply_tokens: int = Field(2000, ge=100, le=32000)
    max_history_turns: int = Field(20, ge=1, le=200)
    max_history_chars: int = Field(24000, ge=1000)
    max_user_message_chars: int = Field(8000, ge=100)

    # --- память ---
    memory_enabled: bool = True
    memory_batch_turns: int = Field(3, ge=1, le=50)  # обрабатывать память каждые N обменов
    memory_max_active: int = Field(300, ge=10)  # сверх лимита в архив уходят наименее ценные
    memory_core_items: int = Field(20, ge=0, le=100)  # сколько главных воспоминаний всегда в контексте

    # --- надёжность ---
    llm_timeout_seconds: float = Field(90, gt=0)
    llm_max_retries: int = Field(2, ge=0, le=5)
    messages_per_minute: int = Field(20, ge=1)
    breaker_failure_threshold: int = Field(3, ge=1)
    breaker_cooldown_seconds: float = Field(60, gt=0)

    backup_dir: Path = PROJECT_ROOT / "backups"
    backup_keep: int = Field(14, ge=1)  # сколько последних копий хранить

    log_level: str = "INFO"

    @field_validator("allowed_telegram_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
            return [int(p) for p in parts]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("allowed_telegram_ids")
    @classmethod
    def _not_empty(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("белый список пуст — бот был бы доступен всем")
        return value

    @field_validator("ollama_base_url", "telegram_api_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def provider_available(self, provider: str) -> bool:
        if provider == "openai":
            return bool(self.openai_api_key and self.openai_api_key.get_secret_value())
        if provider == "anthropic":
            return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value())
        if provider == "ollama":
            return bool(self.ollama_base_url)
        return False


_FIELD_NAMES = {
    "bot_token": "BOT_TOKEN",
    "allowed_telegram_ids": "ALLOWED_TELEGRAM_IDS",
}


def load_settings(**overrides: object) -> Settings:
    """Загружает настройки и превращает ошибки валидации в понятные сообщения."""
    try:
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "?"
            name = _FIELD_NAMES.get(field, field.upper())
            if err["type"] == "missing":
                problems.append(f"{name}: не задано")
            else:
                # Значение не выводим — там может быть секрет.
                problems.append(f"{name}: {err['msg']}")
        raise ConfigError("; ".join(problems)) from None
    if not any(settings.provider_available(p) for p in ("openai", "anthropic", "ollama")):
        raise ConfigError(
            "не задан ни один провайдер моделей: укажите OPENAI_API_KEY, ANTHROPIC_API_KEY "
            "или OLLAMA_BASE_URL"
        )
    return settings
