"""Сквозной прогон: run(), long polling aiogram, миграции, запуск из командной строки."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Callable

import pytest
from pydantic_ai.models.function import FunctionModel

from pai.__main__ import run
from pai.config import PROJECT_ROOT
from pai.llm.engine import PydanticAIEngine
from tests.conftest import OWNER, STRANGER, Script
from tests.fakes import FakeTelegram


async def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("условие не выполнилось вовремя")
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def running(settings, script: Script):
    engine = PydanticAIEngine(settings, model_factory=lambda m: FunctionModel(script.fn, model_name=m.provider_model_name))
    task = asyncio.create_task(run(settings, engine=engine, verify=False))
    yield task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_full_run_and_restart(settings, tmp_path) -> None:
    tg = FakeTelegram()
    await tg.start()
    s = settings.model_copy(update={"telegram_api_url": tg.url})
    script = Script(replies=["Привет! Чем помочь?", "Вас зовут Петя."])
    try:
        async with running(s, script) as task:
            await wait_for(lambda: any(m == "setMyCommands" for m, _ in tg.calls) or task.done())
            if task.done():
                task.result()
            tg.push_text(STRANGER, "привет")
            tg.push_text(OWNER, "/start")
            await wait_for(lambda: len(tg.sent_texts(OWNER)) >= 1 and len(tg.sent_texts(STRANGER)) >= 1)
            tg.push_text(OWNER, "привет, я Петя")
            await wait_for(lambda: "Привет! Чем помочь?" in tg.sent_texts(OWNER))
        async with running(s, script):  # перезапуск: контекст из базы
            tg.push_text(OWNER, "как меня зовут?")
            await wait_for(lambda: "Вас зовут Петя." in tg.sent_texts(OWNER))
        assert script.seen[-1].user_prompts == ["привет, я Петя", "как меня зовут?"]
        assert tg.sent_texts(STRANGER) == ["🔒 Это личный бот. Доступ закрыт."]
        assert len(script.seen) == 2
    finally:
        await tg.stop()


def _cli(env: dict[str, str], cwd=None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT / "src"), "PYDANTIC_AI_NO_BANNER": "1",
            "PAI_ENV_FILE": os.devnull}  # настоящий .env владельца в тестах не читается
    return subprocess.run([sys.executable, "-m", "pai"], env={**base, **env}, capture_output=True,
                          text=True, timeout=60, cwd=cwd or PROJECT_ROOT)


@pytest.mark.parametrize("env,expected", [
    ({}, "BOT_TOKEN: не задано"),
    ({"BOT_TOKEN": "1:x", "ALLOWED_TELEGRAM_IDS": "", "OPENAI_API_KEY": "k"}, "ALLOWED_TELEGRAM_IDS"),
    ({"BOT_TOKEN": "1:x", "ALLOWED_TELEGRAM_IDS": "111"}, "OPENAI_API_KEY"),
])
def test_cli_config_errors_are_clear(env, expected, tmp_path) -> None:
    r = _cli({**env, "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/x.db"})
    assert r.returncode == 2 and expected in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_wrong_bot_token_and_offline_telegram(tmp_path) -> None:
    import threading

    from aiohttp import web

    async def unauthorized(_request):
        return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict = {}

    def serve() -> None:
        asyncio.set_event_loop(loop)
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", unauthorized)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    env = {"BOT_TOKEN": "123:SECRET_TOKEN_VALUE", "ALLOWED_TELEGRAM_IDS": "111", "OPENAI_API_KEY": "sk-secret",
           "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/x.db"}
    try:
        r = _cli({**env, "TELEGRAM_API_URL": holder["url"]})
        assert r.returncode == 2 and "BOT_TOKEN" in r.stderr
        r2 = _cli({**env, "TELEGRAM_API_URL": "http://127.0.0.1:9"})
        assert r2.returncode == 1 and "нет связи с Telegram" in r2.stderr
        for out in (r.stdout + r.stderr, r2.stdout + r2.stderr):
            assert "SECRET_TOKEN_VALUE" not in out and "sk-secret" not in out and "Traceback" not in out
    finally:
        loop.call_soon_threadsafe(loop.stop)
