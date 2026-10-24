"""Configuration loaded from environment variables / ~/.telegram-mcp/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _data_dir() -> Path:
    return Path(os.environ.get("TG_DATA_DIR", "~/.telegram-mcp")).expanduser()


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    data_dir: Path
    session_path: Path
    session_string: str | None
    log_dir: Path
    dry_run: bool
    confirm_mode: str  # "client" | "elicit"
    allowed_chats: frozenset[str] = field(default_factory=frozenset)
    max_messages_per_hour: int = 30
    min_seconds_between_messages_per_chat: int = 20


def load_config() -> Config:
    data_dir = _data_dir()
    # Explicit env vars win over values from the .env file.
    load_dotenv(os.environ.get("TG_ENV_FILE", data_dir / ".env"), override=False)

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            f"TG_API_ID / TG_API_HASH are not set. Put them into {data_dir / '.env'} "
            "(see .env.example) or pass them as environment variables."
        )

    confirm_mode = os.environ.get("TG_CONFIRM_MODE", "client").strip().lower()
    if confirm_mode not in ("client", "elicit"):
        raise RuntimeError("TG_CONFIRM_MODE must be 'client' or 'elicit'")

    allowed = {
        item.strip().lower().lstrip("@")
        for item in os.environ.get("TG_ALLOWED_CHATS", "").split(",")
        if item.strip()
    }

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        data_dir=data_dir,
        session_path=Path(os.environ.get("TG_SESSION_PATH", data_dir / "userbot")).expanduser(),
        session_string=os.environ.get("TG_SESSION_STRING") or None,
        log_dir=Path(os.environ.get("TG_LOG_DIR", data_dir / "logs")).expanduser(),
        dry_run=_bool("TG_DRY_RUN", False),
        confirm_mode=confirm_mode,
        allowed_chats=frozenset(allowed),
        max_messages_per_hour=int(os.environ.get("TG_MAX_MESSAGES_PER_HOUR", "30")),
        min_seconds_between_messages_per_chat=int(
            os.environ.get("TG_MIN_SECONDS_BETWEEN_MESSAGES_PER_CHAT", "20")
        ),
    )


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
