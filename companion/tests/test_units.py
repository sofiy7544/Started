"""Модульные тесты без БД: конфигурация, персонаж, фильтр данных, промпты, лимиты, цены."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from companion import pricing
from companion.bot.app import split_message
from companion.config import PROJECT_ROOT, ConfigError, Settings
from companion.llm.client import LLMErrorKind
from companion.llm.prompts import build_messages, build_user_context
from companion.persona import CORE_RULES, load_persona
from companion.repositories.memory import Fact, StoredMessage
from companion.services.chat import trim_to_sentence
from companion.services.limits import CircuitBreaker, RateLimiter
from companion.services.sensitive import is_sensitive, redact
from tests.conftest import Clock

BASE_ENV = {"BOT_TOKEN": "1:abc", "DATABASE_URL": "postgresql://x"}


# ---- конфигурация ---------------------------------------------------------------------------

def test_config_requires_secrets_from_env() -> None:
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        Settings.from_env({"DATABASE_URL": "x"})
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env({"BOT_TOKEN": "x"})


def test_config_defaults_use_cheap_model_and_limits() -> None:
    s = Settings.from_env(BASE_ENV)
    assert s.chat_model == "claude-haiku-5-5"
    assert s.max_reply_tokens == 1000
    assert s.anthropic_monthly_budget_usd == 20.0
    assert s.llm_max_retries == 2


def test_config_parses_types_and_validates() -> None:
    s = Settings.from_env({**BASE_ENV, "MAX_REPLY_TOKENS": "300", "LLM_REFUSAL_FALLBACKS": "off",
                           "ANTHROPIC_MONTHLY_BUDGET_USD": "5.5", "PERSONA_PATH": "/tmp/p.toml"})
    assert (s.max_reply_tokens, s.llm_refusal_fallbacks, s.anthropic_monthly_budget_usd) == (300, False, 5.5)
    assert isinstance(s.persona_path, Path)
    with pytest.raises(ConfigError, match="MAX_REPLY_TOKENS"):
        Settings.from_env({**BASE_ENV, "MAX_REPLY_TOKENS": "много"})
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE_ENV, "KEEP_RECENT_MESSAGES": "50"})
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE_ENV, "LLM_MAX_RETRIES": "100"})


def test_no_secrets_in_tracked_files() -> None:
    repo = PROJECT_ROOT.parent
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    patterns = [re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")]
    for name in files:
        path = repo / name
        if path.suffix in {".png", ".jpg", ".ogg", ".mp4"} or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for p in patterns:
            assert not p.search(content), f"похоже на секрет в {name}"


def test_gitignore_excludes_env_and_logs() -> None:
    ignored = (PROJECT_ROOT.parent / ".gitignore").read_text() + (PROJECT_ROOT / ".gitignore").read_text()
    for entry in (".env", "logs/", "*.sql.gz", "backups/"):
        assert entry in ignored


# ---- персонаж -------------------------------------------------------------------------------

def test_persona_file_loads_and_includes_core_rules(persona) -> None:
    prompt = persona.system_prompt()
    assert persona.name in prompt
    assert CORE_RULES in prompt
    assert "AI-персонаж" in prompt


def test_persona_must_be_adult(tmp_path: Path) -> None:
    src = (PROJECT_ROOT / "persona" / "mira.toml").read_text(encoding="utf-8")
    bad = tmp_path / "p.toml"
    bad.write_text(src.replace("age = 26", "age = 17"), encoding="utf-8")
    with pytest.raises(ConfigError, match="совершеннолет"):
        load_persona(bad)
    bad.write_text('name = "X"', encoding="utf-8")
    with pytest.raises(ConfigError, match="age"):
        load_persona(bad)
    with pytest.raises(ConfigError, match="не найден"):
        load_persona(tmp_path / "missing.toml")


# ---- чувствительные данные ------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "моя карта 4111 1111 1111 1111", "код из смс 482913", "пароль: Qwerty!23",
    "звони +7 (999) 123-45-67", "пиши на me@example.com", "паспорт 45 12 345678",
])
def test_sensitive_data_is_masked(text: str) -> None:
    assert redact(text) != text
    assert is_sensitive(text)


@pytest.mark.parametrize("text", [
    "я пишу код на питоне", "Любит джаз", "в 2024 2025 годах", "мне 25 лет", "Есть кот Барсик",
])
def test_ordinary_text_is_untouched(text: str) -> None:
    assert redact(text) == text
    assert not is_sensitive(text)


# ---- промпты --------------------------------------------------------------------------------

T0 = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


def _msg(i: int, role: str, text: str, minutes: int = 0) -> StoredMessage:
    return StoredMessage(i, role, text, T0 + timedelta(minutes=minutes))


def test_build_messages_limits_chars_and_starts_with_user() -> None:
    history = [_msg(1, "user", "a" * 50), _msg(2, "assistant", "b" * 50),
               _msg(3, "user", "c" * 50), _msg(4, "assistant", "d" * 50), _msg(5, "user", "e" * 50)]
    msgs = build_messages(history, max_chars=120)
    assert [m["content"][0] for m in msgs] == ["e"] or msgs[0]["role"] == "user"
    assert sum(len(m["content"]) for m in msgs) <= 120
    assert msgs[-1]["content"] == "e" * 50
    assert msgs[0]["role"] == "user"


def test_user_context_hints_greeting_and_variety() -> None:
    hist = [_msg(1, "user", "привет"), _msg(2, "assistant", "привет! как ты?"),
            _msg(3, "user", "норм"), _msg(4, "assistant", "а что делал?"), _msg(5, "user", "работал", 3)]
    ctx = build_user_context(first_name="Петя", facts=[Fact(1, "interest", "Любит джаз")],
                             summaries=[], history=hist, previous_message=hist[-2], now=T0)
    assert "не здоровайся заново" in ctx
    assert "Любит джаз" in ctx
    assert "без вопроса" in ctx  # два последних ответа заканчивались вопросом

    long_pause = [_msg(1, "user", "я вернулся", 60 * 24)]
    ctx = build_user_context(first_name=None, facts=[], summaries=[], history=long_pause,
                             previous_message=_msg(0, "assistant", "пока"), now=T0)
    assert "после перерыва" in ctx
    assert "мало знакомы" in ctx


def test_preferred_name_overrides_profile_name() -> None:
    ctx = build_user_context(first_name="Alexander", facts=[Fact(1, "name", "Саша")], summaries=[],
                             history=[_msg(1, "user", "hi")], previous_message=None, now=T0)
    assert "обращаться к нему: Саша" in ctx
    assert "Alexander" not in ctx


# ---- лимиты ---------------------------------------------------------------------------------

def test_rate_limiter_window_and_single_warning() -> None:
    clock = Clock()
    rl = RateLimiter(2, clock=clock)
    assert rl.check(1) == (True, False)
    assert rl.check(1) == (True, False)
    assert rl.check(1) == (False, True)   # предупреждение один раз
    assert rl.check(1) == (False, False)
    assert rl.check(2) == (True, False)   # другой пользователь не затронут
    clock.t += 61
    assert rl.check(1) == (True, False)


def test_circuit_breaker_opens_and_recovers() -> None:
    clock = Clock()
    br = CircuitBreaker(threshold=2, cooldown=60, clock=clock)
    br.failure(LLMErrorKind.TEMPORARY)
    assert br.allow()
    br.failure(LLMErrorKind.TEMPORARY)
    assert not br.allow()
    clock.t += 61
    assert br.allow()
    br.failure(LLMErrorKind.AUTH)
    clock.t += 61
    assert not br.allow()  # ошибка ключа — пауза дольше
    clock.t += 600
    assert br.allow()


# ---- прочее ---------------------------------------------------------------------------------

def test_pricing_matches_official_table() -> None:
    """Цены с https://platform.claude.com/docs/en/about-claude/pricing (2026-10-27)."""
    c = pricing.cost_usd
    # Haiku 5.5, промпт до 100 000 токенов: $0.10 вход, $0.50 выход, $0.01 кэш-чтение, $0.125 запись.
    assert c("claude-haiku-5-5", input_tokens=10_000, output_tokens=1_000) == Decimal("0.0015")
    assert c("claude-haiku-5-5", input_tokens=0, output_tokens=0, cache_read_tokens=50_000) == Decimal("0.0005")
    assert c("claude-haiku-5-5", input_tokens=0, output_tokens=0, cache_write_tokens=80_000) == Decimal("0.01")
    # Больше 100 000 токенов промпта (с учётом кэша) — весь запрос по $0.50 / $2.50.
    assert c("claude-haiku-5-5", input_tokens=60_000, output_tokens=1_000,
             cache_read_tokens=50_000) == Decimal("0.035")  # 0.03 + 0.0025 + 0.0025
    # Sonnet 5.5: $2 / $10, кэш-чтение $0.10 (0.05x), запись $2.50.
    assert c("claude-sonnet-5-5", input_tokens=0, output_tokens=100_000) == Decimal("1")
    assert c("claude-sonnet-5-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000) == Decimal("0.1")
    # Opus 5.5: $4 / $20, кэш-чтение $0.20.
    assert c("claude-opus-5-5", input_tokens=50_000, output_tokens=15_000) == Decimal("0.5")


def test_unknown_model_is_priced_conservatively() -> None:
    assert not pricing.is_known("claude-something-new")
    assert pricing.cost_usd("claude-something-new", input_tokens=100_000, output_tokens=0) == Decimal("1")


def test_split_message_respects_telegram_limit() -> None:
    text = ("Предложение номер один. " * 400).strip()
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert " ".join(parts).replace("  ", " ") == text
    assert split_message("коротко") == ["коротко"]
    assert all(len(p) <= 4096 for p in split_message("x" * 9000))


def test_trim_to_sentence() -> None:
    assert trim_to_sentence("Первая мысль. Вторая мысль, которая оборвал") == "Первая мысль."
    assert trim_to_sentence("без точек вообще") == "без точек вообще…"


def test_safe_formatter_hides_exception_messages() -> None:
    import logging

    from companion.logging_setup import SafeFormatter

    try:
        try:
            raise ValueError("DETAIL: Key (user_id, fact_norm)=(5, любит джаз)")
        except ValueError as inner:
            raise RuntimeError("input_value='мой секрет'") from inner
    except RuntimeError:
        import sys
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "ошибка", None, sys.exc_info())
    out = SafeFormatter("%(message)s").format(record)
    assert "ошибка" in out and "RuntimeError" in out and "ValueError" in out
    assert "любит джаз" not in out and "мой секрет" not in out
    assert "test_units.py" in out  # место ошибки сохраняется для отладки
