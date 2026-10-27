"""Подделки для тестов: LLM-клиент в памяти, HTTP-серверы Telegram Bot API и Anthropic API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from companion.llm.client import LLMError, LLMResponse, MemoryExtraction, Usage


@dataclass
class ChatCall:
    model: str
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    max_tokens: int

    @property
    def everything(self) -> str:
        """Весь текст, ушедший в модель, — для проверок изоляции."""
        return "\n".join([b["text"] for b in self.system] + [m["content"] for m in self.messages])


@dataclass
class FakeLLM:
    replies: list[str | LLMResponse | Exception] = field(default_factory=list)
    extractions: list[MemoryExtraction | Exception | None] = field(default_factory=list)
    chat_calls: list[ChatCall] = field(default_factory=list)
    memory_calls: list[str] = field(default_factory=list)
    delay: float = 0.0
    reply_fn: Callable[[ChatCall], str] | None = None

    async def chat(self, *, model, system, messages, max_tokens) -> LLMResponse:
        call = ChatCall(model, system, messages, max_tokens)
        self.chat_calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        item: str | LLMResponse | Exception
        if self.replies:
            item = self.replies.pop(0)
        elif self.reply_fn:
            item = self.reply_fn(call)
        else:
            item = f"ответ {len(self.chat_calls)}"
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(item, model, model, Usage(1000, 50))

    async def extract_memory(self, *, model, system, transcript, max_tokens):
        self.memory_calls.append(transcript)
        item = self.extractions.pop(0) if self.extractions else MemoryExtraction(summary="Болтали.")
        if isinstance(item, LLMError):
            raise item
        return item, LLMResponse("", model, model, Usage(2000, 200))


# ---------------------------------------------------------------- Telegram Bot API ----------


class FakeTelegram:
    """Минимальный Bot API: отдаёт апдейты из очереди и записывает исходящие вызовы."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_update = 1
        self._next_message = 1000
        self.runner: web.AppRunner | None = None
        self.url = ""

    def push_text(self, user_id: int, text: str, first_name: str = "Тест") -> int:
        uid = self._next_update
        self._next_update += 1
        self.updates.put_nowait({
            "update_id": uid,
            "message": {
                "message_id": uid, "date": 1_700_000_000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": first_name},
                "text": text,
                **({"entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
                   if text.startswith("/") else {}),
            },
        })
        return uid

    def push_callback(self, user_id: int, data: str, message_id: int = 1) -> None:
        uid = self._next_update
        self._next_update += 1
        self.updates.put_nowait({
            "update_id": uid,
            "callback_query": {
                "id": str(uid), "chat_instance": "ci", "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
                "message": {"message_id": message_id, "date": 1_700_000_000,
                            "chat": {"id": user_id, "type": "private"}, "text": "..."},
            },
        })

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        return [
            p["text"] for m, p in self.calls
            if m in ("sendMessage", "editMessageText") and (chat_id is None or int(p["chat_id"]) == chat_id)
        ]

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = dict(await request.post())
        if method != "getUpdates":
            self.calls.append((method, params))
        result: Any = True
        if method == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Mira", "username": "mira_test_bot"}
        elif method == "getUpdates":
            batch = []
            try:
                batch.append(await asyncio.wait_for(self.updates.get(), timeout=0.2))
                while not self.updates.empty():
                    batch.append(self.updates.get_nowait())
            except TimeoutError:
                pass
            result = batch
        elif method in ("sendMessage", "editMessageText"):
            self._next_message += 1
            result = {"message_id": self._next_message, "date": 1_700_000_000,
                      "chat": {"id": int(params["chat_id"]), "type": "private"},
                      "text": params.get("text", "")}
        return web.json_response({"ok": True, "result": result})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


# ---------------------------------------------------------------- Anthropic API -------------


class FakeAnthropic:
    """Отвечает как POST /v1/messages. `statuses` — коды ответов по очереди (пусто = 200)."""

    def __init__(self, text: str = "привет, рада тебя видеть") -> None:
        self.text = text
        self.statuses: list[int] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.stop_reason = "end_turn"
        self.memory_json = {"summary": "Познакомились.", "preferred_name": None,
                            "new_facts": [], "obsolete_fact_ids": []}
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            err = {401: "authentication_error", 400: "invalid_request_error",
                   429: "rate_limit_error", 529: "overloaded_error"}.get(status, "api_error")
            return web.json_response(
                {"type": "error", "error": {"type": err, "message": "fake"}}, status=status,
                headers={"retry-after": "0"},
            )
        is_memory = "format" in (body.get("output_config") or {})
        text = json.dumps(self.memory_json, ensure_ascii=False) if is_memory else self.text
        return web.json_response({
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}],
            "stop_reason": self.stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 40,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        })

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/messages", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
