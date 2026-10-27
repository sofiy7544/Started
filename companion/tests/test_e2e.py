"""Сквозной прогон приложения: настоящие run(), aiogram long polling, Anthropic SDK, PostgreSQL.
Telegram и Anthropic подменены локальными HTTP-серверами — сеть и деньги не тратятся."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Callable

import pytest

from companion import texts
from companion.__main__ import run
from companion.config import PROJECT_ROOT, Settings
from companion.persona import load_persona
from tests.conftest import TEST_DB
from tests.fakes import FakeAnthropic, FakeTelegram

USER = 5001


async def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не выполнилось вовремя")
        await asyncio.sleep(0.05)


@pytest.fixture
async def servers(monkeypatch, pool):
    tg, anth = FakeTelegram(), FakeAnthropic()
    await tg.start()
    await anth.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", anth.url)
    yield tg, anth
    await tg.stop()
    await anth.stop()


@contextlib.asynccontextmanager
async def running_app(tg: FakeTelegram):
    settings = Settings(bot_token="123456:TEST", database_url=TEST_DB, telegram_api_url=tg.url,
                        llm_max_retries=0)
    task = asyncio.create_task(run(settings))
    try:
        await wait_for(lambda: any(m == "setMyCommands" for m, _ in tg.calls) or task.done())
        if task.done():
            task.result()  # пробросить ошибку старта
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_full_dialog_and_restart(servers) -> None:
    tg, anth = servers
    persona = load_persona(PROJECT_ROOT / "persona" / "mira.toml")

    async with running_app(tg):
        tg.push_text(USER, "/start")
        await wait_for(lambda: len(tg.sent_texts(USER)) >= 1)
        assert "AI-персонаж" in tg.sent_texts(USER)[0]
        tg.push_callback(USER, "age:ok")
        await wait_for(lambda: texts.after_confirm(persona) in tg.sent_texts(USER))
        anth.text = "привет! я Мира, рада знакомству"
        tg.push_text(USER, "привет, я Олег")
        await wait_for(lambda: anth.text in tg.sent_texts(USER))
        commands = [p for m, p in tg.calls if m == "setMyCommands"][0]["commands"]
        assert '"memory"' in commands and '"forget"' in commands

    # «Перезапуск»: новый процесс приложения на той же БД помнит переписку.
    async with running_app(tg):
        anth.text = "конечно помню, Олег"
        tg.push_text(USER, "помнишь меня?")
        await wait_for(lambda: anth.text in tg.sent_texts(USER))
    sent = [m["content"] for m in anth.requests[-1]["messages"]]
    assert sent == ["привет, я Олег", "привет! я Мира, рада знакомству", "помнишь меня?"]
    assert anth.requests[-1]["model"] == "claude-haiku-5-5"
    assert anth.requests[-1]["max_tokens"] == 1000


async def test_invalid_api_key_gives_user_friendly_message(servers) -> None:
    tg, anth = servers
    anth.statuses = [401] * 10
    async with running_app(tg):
        tg.push_text(USER, "/start")
        await wait_for(lambda: len(tg.sent_texts(USER)) >= 1)
        tg.push_callback(USER, "age:ok")
        await wait_for(lambda: len(tg.sent_texts(USER)) >= 2)
        tg.push_text(USER, "привет")
        await wait_for(lambda: texts.UNAVAILABLE in tg.sent_texts(USER))
        tg.push_text(USER, "ау")
        await wait_for(lambda: tg.sent_texts(USER).count(texts.UNAVAILABLE) == 2)
    assert len(anth.requests) == 1  # после 401 API больше не вызывается (предохранитель)


def _run_cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "companion"], env={**base, **env},
                          capture_output=True, text=True, timeout=30)


def test_cli_reports_missing_configuration() -> None:
    r = _run_cli({})
    assert r.returncode == 2 and "BOT_TOKEN" in r.stderr and "Traceback" not in r.stderr
    r = _run_cli({"BOT_TOKEN": "1:x", "DATABASE_URL": TEST_DB})
    assert r.returncode == 2 and "ANTHROPIC_API_KEY" in r.stderr


async def test_wrong_bot_token_is_clear_config_error(servers) -> None:
    from aiohttp import web

    from companion.config import ConfigError

    tg, _ = servers

    async def unauthorized(request: web.Request) -> web.Response:
        return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", unauthorized)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        settings = Settings(bot_token="1:BAD", database_url=TEST_DB, telegram_api_url=f"http://127.0.0.1:{port}")
        with pytest.raises(ConfigError, match="BOT_TOKEN"):
            await run(settings)
    finally:
        await runner.cleanup()


def test_cli_reports_unreachable_database() -> None:
    r = _run_cli({"BOT_TOKEN": "1:x", "ANTHROPIC_API_KEY": "x",
                  "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db"})
    assert r.returncode == 1 and "базой данных" in r.stderr and "Traceback" not in r.stderr


async def test_wrong_model_id_fails_at_startup_without_spending(servers) -> None:
    from companion.config import ConfigError

    tg, anth = servers
    settings = Settings(bot_token="123456:TEST", database_url=TEST_DB, telegram_api_url=tg.url,
                        chat_model="claude-haiku-9-9")
    with pytest.raises(ConfigError, match="claude-haiku-9-9"):
        await run(settings)
    assert anth.requests == []  # платных запросов не было — только бесплатный Models API
    assert anth.model_lookups == ["claude-haiku-9-9"]


async def test_wrong_api_key_fails_at_startup(servers) -> None:
    from companion.config import ConfigError

    tg, anth = servers
    anth.models_status = 401
    settings = Settings(bot_token="123456:TEST", database_url=TEST_DB, telegram_api_url=tg.url)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        await run(settings)
    assert anth.requests == []


def test_cli_reports_unreachable_telegram() -> None:
    r = _run_cli({"BOT_TOKEN": "1:x", "ANTHROPIC_API_KEY": "x", "DATABASE_URL": TEST_DB,
                  "TELEGRAM_API_URL": "http://127.0.0.1:9"})
    assert r.returncode == 1 and "нет связи с Telegram" in r.stderr
    assert "Traceback" not in r.stderr and "1:x" not in r.stderr  # токен не выводится


def test_cli_wrong_key_and_model_messages() -> None:
    """Через настоящий CLI: понятное сообщение, код 2, без traceback и без секретов."""
    import threading

    loop = asyncio.new_event_loop()
    tg, anth = FakeTelegram(), FakeAnthropic()
    anth.models_status = 401
    started = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(tg.start())
        loop.run_until_complete(anth.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    started.wait(5)
    try:
        env = {"BOT_TOKEN": "123:SECRETTOKEN", "ANTHROPIC_API_KEY": "sk-secret-key",
               "DATABASE_URL": TEST_DB, "TELEGRAM_API_URL": tg.url, "ANTHROPIC_BASE_URL": anth.url}
        r = _run_cli(env)
        assert r.returncode == 2 and "ANTHROPIC_API_KEY" in r.stderr
        anth.models_status = 200
        r = _run_cli({**env, "CHAT_MODEL": "claude-nonexistent"})
        assert r.returncode == 2 and "claude-nonexistent" in r.stderr
        for out in (r.stdout, r.stderr):
            assert "SECRETTOKEN" not in out and "sk-secret-key" not in out and "Traceback" not in out
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)


def test_live_check_script_runs_against_fake_api() -> None:
    """Скрипт живой проверки работает (здесь — против фейкового API, без затрат)."""
    import threading

    loop = asyncio.new_event_loop()
    anth = FakeAnthropic(text="Я виртуальная собеседница на основе ИИ, Саша. Про поход и горы помню.")
    started = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(anth.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    started.wait(5)
    try:
        base = {"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT / "src"),
                "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": anth.url}
        script = str(PROJECT_ROOT / "scripts" / "live_check.py")
        r = subprocess.run([sys.executable, script], env=base, capture_output=True, text=True, timeout=30)
        assert r.returncode == 1 and "--yes" in r.stdout and anth.requests == []  # без --yes ни одного запроса
        r = subprocess.run([sys.executable, script, "--yes"], env=base, capture_output=True, text=True, timeout=60)
        assert "Итого:" in r.stdout and len(anth.requests) == 7
        assert "[«ты настоящая?»]" in r.stdout
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
