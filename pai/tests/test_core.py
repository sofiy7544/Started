"""Конфигурация, каталог моделей, классификация ошибок, цены."""

from __future__ import annotations

import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai import exceptions as E

from pai.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from pai.llm.catalog import load_catalog
from pai.llm.engine import FailureKind, classify

BASE = {"_env_file": None, "bot_token": "1:x", "allowed_telegram_ids": "111", "openai_api_key": "k"}


def test_settings_require_token_and_whitelist() -> None:
    with pytest.raises(ConfigError, match="BOT_TOKEN: не задано"):
        load_settings(**{k: v for k, v in BASE.items() if k != "bot_token"})
    with pytest.raises(ConfigError, match="ALLOWED_TELEGRAM_IDS"):
        load_settings(**{**BASE, "allowed_telegram_ids": ""})
    with pytest.raises(ConfigError, match="провайдер"):
        load_settings(**{k: v for k, v in BASE.items() if k != "openai_api_key"})


def test_whitelist_parsing() -> None:
    s = load_settings(**{**BASE, "allowed_telegram_ids": "111, 222;333"})
    assert s.allowed_telegram_ids == [111, 222, 333]
    with pytest.raises(ConfigError, match="ALLOWED_TELEGRAM_IDS"):
        load_settings(**{**BASE, "allowed_telegram_ids": "abc"})


def test_config_errors_never_print_secret_values() -> None:
    secret = "sk-very-secret-value"
    with pytest.raises(ConfigError) as err:
        load_settings(**{**BASE, "openai_api_key": secret, "max_reply_tokens": "много"})
    assert secret not in str(err.value)
    assert "MAX_REPLY_TOKENS" in str(err.value)
    s = Settings(**{**BASE, "openai_api_key": secret})
    assert secret not in repr(s) and secret not in str(s.model_dump())


def test_catalog_availability_follows_keys() -> None:
    cat = load_catalog(PROJECT_ROOT / "config" / "models.toml")
    only_openai = load_settings(**BASE)
    assert {m.provider for m in cat.available(only_openai)} == {"openai"}
    both = load_settings(**{**BASE, "anthropic_api_key": "a", "ollama_base_url": "http://localhost:11434/v1"})
    assert {m.provider for m in cat.available(both)} == {"openai", "anthropic", "ollama"}
    # Недоступная или неизвестная модель → первая доступная из цепочки default.
    assert cat.resolve("claude-sonnet-5-5", only_openai).id == "gpt-6-luna"
    assert cat.resolve("нет-такой", only_openai).id == "gpt-6-luna"
    assert cat.resolve(None, both).id == "claude-opus-5-5"  # с ключом Claude — Claude по умолчанию
    assert cat.memory_model(both).id == "claude-haiku-5-5"
    assert cat.memory_model(only_openai).id == "gpt-6-luna"


def test_catalog_validation(tmp_path: Path) -> None:
    bad = tmp_path / "m.toml"
    bad.write_text('[[model]]\nid="x"\nprovider="mystery"\napi_model="a:b"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="неизвестный провайдер"):
        load_catalog(bad)
    bad.write_text('default="y"\n[[model]]\nid="x"\nprovider="openai"\napi_model="openai:x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="'default'"):
        load_catalog(bad)
    with pytest.raises(ConfigError, match="не найден"):
        load_catalog(tmp_path / "nope.toml")


def test_prices_match_official_anthropic_table() -> None:
    """Сверка genai-prices с https://platform.claude.com/docs/en/about-claude/pricing (2026-11-04)."""
    cat = load_catalog(PROJECT_ROOT / "config" / "models.toml")
    haiku, sonnet = cat.get("claude-haiku-5-5"), cat.get("claude-sonnet-5-5")
    assert haiku.cost_usd(input_tokens=1_000, output_tokens=0) == Decimal("0.0001")  # $0.10 / 1M
    assert haiku.cost_usd(input_tokens=0, output_tokens=1_000) == Decimal("0.0005")  # $0.50 / 1M
    assert sonnet.cost_usd(input_tokens=10_000, output_tokens=1_000) == Decimal("0.03")  # $2 + $10
    # input_tokens включает кэш: 10k токенов, из них 10k из кэша → по цене кэша ($0.10 у Sonnet).
    assert sonnet.cost_usd(input_tokens=10_000, output_tokens=0, cache_read_tokens=10_000) == Decimal("0.001")


def test_local_and_unknown_model_prices(tmp_path: Path) -> None:
    cat = load_catalog(PROJECT_ROOT / "config" / "models.toml")
    assert cat.get("ollama-local").cost_usd(input_tokens=10**6, output_tokens=10**6) == 0
    f = tmp_path / "m.toml"
    f.write_text('[[model]]\nid="x"\nprovider="openai"\napi_model="openai:totally-new-model"\n', encoding="utf-8")
    unknown = load_catalog(f).get("x")
    assert not unknown.price_known()
    assert unknown.cost_usd(input_tokens=1_000_000, output_tokens=0) == Decimal("15")  # с запасом


@pytest.mark.parametrize("exc,kind", [
    (E.ModelAuthenticationError("m", "bad key"), FailureKind.AUTH),
    (E.ModelQuotaExceededError("m", "no money"), FailureKind.QUOTA),
    (E.ModelRateLimitError("m", "slow down"), FailureKind.RATE_LIMIT),
    (E.ModelTimeoutError("m", "timeout"), FailureKind.TIMEOUT),
    (E.ModelConnectionError("m", "net"), FailureKind.TEMPORARY),
    (E.ModelOverloadedError("m", "busy"), FailureKind.TEMPORARY),
    (E.ModelNotFoundError("m", "no model"), FailureKind.MODEL_NOT_FOUND),
    (E.ModelContextWindowExceededError("m", "too long"), FailureKind.CONTEXT_TOO_LONG),
    (E.ContentFilterError("filtered"), FailureKind.REFUSED),
    (E.UnexpectedModelBehavior("weird"), FailureKind.BAD_RESPONSE),
    (E.UsageLimitExceeded("too many"), FailureKind.TOO_MANY_STEPS),
    (E.ModelHTTPError(401, "m"), FailureKind.AUTH),
    (E.ModelHTTPError(429, "m"), FailureKind.RATE_LIMIT),
    (E.ModelHTTPError(503, "m"), FailureKind.TEMPORARY),
    (E.UserError("no api key"), FailureKind.CONFIG),
])
def test_error_classification(exc: Exception, kind: FailureKind) -> None:
    failure = classify(exc)
    assert failure.kind is kind
    assert "bad key" not in str(failure) and "no money" not in str(failure)  # без текста провайдера


def test_possibly_billed_kinds() -> None:
    assert classify(E.ModelTimeoutError("m", "t")).possibly_billed
    assert classify(E.UnexpectedModelBehavior("x")).possibly_billed
    assert not classify(E.ModelAuthenticationError("m", "x")).possibly_billed
    assert not classify(E.ModelRateLimitError("m", "x")).possibly_billed


def test_no_secrets_in_repository() -> None:
    repo = PROJECT_ROOT.parent
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                           cwd=repo, capture_output=True, text=True, check=True).stdout.split()
    patterns = [re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), re.compile(r"sk-(proj-)?[A-Za-z0-9_-]{30,}"),
                re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")]
    for name in files:
        path = repo / name
        if not path.is_file() or path.suffix in {".png", ".jpg", ".ogg", ".db"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for p in patterns:
            assert not p.search(content), f"похоже на секрет в {name}"


def test_gitignore_protects_secrets_and_data() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text()
    for entry in (".env", "data/", "*.db", "logs/"):
        assert entry in ignored
    root = subprocess.run(["git", "check-ignore", "-q", str(PROJECT_ROOT / ".env")], cwd=PROJECT_ROOT)
    assert root.returncode == 0
