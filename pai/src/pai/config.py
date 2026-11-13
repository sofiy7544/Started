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
    # Отдельный бот для рабочей группы (контроль обучения): свой токен и своя база — к личным
    # данным владельца (переписка, память, задачи) у него нет доступа. Запуск: python -m pai group
    training_bot_token: SecretStr | None = None
    training_database_url: str = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'training.db').as_posix()}"

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

    # --- голос ---
    voice_enabled: bool = True
    stt_provider: str = "auto"  # auto | openai | local
    tts_provider: str = "auto"  # auto | openai | edge | none
    stt_local_model: str = "small"  # faster-whisper: tiny | base | small | medium | large-v3
    max_voice_seconds: int = Field(900, ge=5, le=3600)  # длиннее — не распознаём (расходы)
    max_tts_chars: int = Field(1500, ge=100, le=4000)  # ≈ 2 минуты речи; остальное — текстом

    # --- вложения: фото, документы, аудио, видео ---
    media_enabled: bool = True
    media_dir: Path = PROJECT_ROOT / "data" / "media"
    max_file_mb: int = Field(20, ge=1, le=2000)  # облачный Bot API отдаёт до 20 МБ
    max_media_seconds: int = Field(1800, ge=10, le=14400)  # аудио/видео: распознаём не дольше
    max_video_frames: int = Field(10, ge=3, le=30)
    max_images_per_request: int = Field(12, ge=1, le=40)
    max_ocr_pages: int = Field(6, ge=0, le=30)  # страниц-сканов PDF сразу в запрос
    media_inline_chars: int = Field(30000, ge=2000, le=200000)  # текста вложений сразу в запрос
    media_retention_hours: float = Field(24, gt=0)  # сколько хранить сами файлы
    media_wait_seconds: float = Field(4.0, ge=0)  # ждать подпись/вопрос после файла
    media_text_wait_seconds: float = Field(1.5, ge=0)  # ждать ещё файлы после вопроса
    max_files_per_message: int = Field(20, ge=1, le=100)

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

    @field_validator("stt_provider", "tts_provider")
    @classmethod
    def _voice_provider(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        allowed = {"stt_provider": ("auto", "openai", "local"),
                   "tts_provider": ("auto", "openai", "edge", "none")}[info.field_name]
        if value not in allowed:
            raise ValueError(f"допустимо: {', '.join(allowed)}")
        return value

    @field_validator("ollama_base_url", "telegram_api_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def max_file_bytes(self) -> int:
        limit = self.max_file_mb * 1024 * 1024
        return limit if self.telegram_api_url else min(limit, 20 * 1024 * 1024)

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
