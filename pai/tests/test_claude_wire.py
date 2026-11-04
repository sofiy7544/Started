"""Формат запроса к Claude API: настоящий pydantic-ai + Anthropic SDK против локального сервера."""

from __future__ import annotations

from decimal import Decimal

import pytest
from aiohttp import web

from pai.config import PROJECT_ROOT
from pai.llm.catalog import load_catalog
from pai.llm.engine import FailureKind, LLMFailure, PydanticAIEngine


class FakeAnthropicAPI:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.status = 200
        self.stop_reason = "end_turn"

    async def messages(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.json())
        self.headers.append(dict(request.headers))
        if self.status != 200:
            err = {401: "authentication_error", 529: "overloaded_error"}.get(self.status, "api_error")
            return web.json_response({"type": "error", "error": {"type": err, "message": "x"}},
                                     status=self.status)
        body = self.bodies[-1]
        return web.json_response({
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "Привет! Я на Claude."}],
            "stop_reason": self.stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 30,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        })

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/messages", self.messages)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@pytest.fixture
async def claude(monkeypatch, settings):
    api = FakeAnthropicAPI()
    url = await api.start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    from pydantic import SecretStr
    s = settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test"), "llm_max_retries": 0})
    yield api, s
    await api.runner.cleanup()


async def test_opus_request_format(claude) -> None:
    api, s = claude
    model = load_catalog(PROJECT_ROOT / "config" / "models.toml").get("claude-opus-5-5")
    reply = await PydanticAIEngine(s).reply(
        model, prompt="привет", history=[], instructions="Ты ассистент.", max_tokens=500,
        cost_limit_usd=Decimal("0.1"),
    )
    assert reply.text == "Привет! Я на Claude."
    # Цена Opus 5.5 $4/$20: 1200×4/1M + 30×20/1M
    assert reply.cost_usd == Decimal("0.0054")
    body, headers = api.bodies[0], api.headers[0]
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 500
    assert body["output_config"]["effort"] == "low"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert "temperature" not in body and "budget_tokens" not in str(body.get("thinking"))
    system = body["system"]
    assert isinstance(system, list) and system[-1].get("cache_control", {}).get("type") == "ephemeral"
    # Anthropic SDK 2.x передаёт ключ API как Authorization: Bearer (x-api-key — старые версии).
    assert headers.get("Authorization") == "Bearer sk-test"


async def test_haiku_has_no_fallback_param(claude) -> None:
    api, s = claude
    model = load_catalog(PROJECT_ROOT / "config" / "models.toml").get("claude-haiku-5-5")
    await PydanticAIEngine(s).reply(model, prompt="hi", history=[], instructions="x", max_tokens=100,
                                    cost_limit_usd=Decimal("0.1"))
    assert "fallbacks" not in api.bodies[0]  # у Haiku 5.5 серверного fallback нет


async def test_claude_errors_are_classified(claude) -> None:
    api, s = claude
    model = load_catalog(PROJECT_ROOT / "config" / "models.toml").get("claude-sonnet-5-5")
    engine = PydanticAIEngine(s)
    api.status = 401
    with pytest.raises(LLMFailure) as err:
        await engine.reply(model, prompt="hi", history=[], instructions="x", max_tokens=100,
                           cost_limit_usd=Decimal("0.1"))
    assert err.value.kind is FailureKind.AUTH
    api.status, api.stop_reason = 200, "refusal"
    with pytest.raises(LLMFailure) as err:
        await engine.reply(model, prompt="hi", history=[], instructions="x", max_tokens=100,
                           cost_limit_usd=Decimal("0.1"))
    assert err.value.kind is FailureKind.REFUSED  # частичный текст не показываем
    assert err.value.billed is not None and err.value.billed.cost_usd > 0  # фактический расход известен
