"""Фейковый Telegram Bot API по HTTP — для тестов без сети (перенесён из проекта companion)."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web


class FakeTelegram:
    """Минимальный Bot API: отдаёт апдейты из очереди и записывает исходящие вызовы."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_update = 1
        self._next_message = 1000
        self.files: dict[str, bytes] = {}  # file_id → содержимое (для getFile и скачивания)
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
        elif method == "getFile":
            fid = params["file_id"]
            result = {"file_id": fid, "file_unique_id": fid, "file_size": len(self.files.get(fid, b"")),
                      "file_path": f"voice/{fid}.oga"}
        elif method in ("sendMessage", "editMessageText", "sendDocument", "sendVoice"):
            self._next_message += 1
            result = {"message_id": self._next_message, "date": 1_700_000_000,
                      "chat": {"id": int(params["chat_id"]), "type": "private"},
                      "text": params.get("text", "")}
        return web.json_response({"ok": True, "result": result})

    async def _download(self, request: web.Request) -> web.Response:
        fid = request.match_info["path"].split("/")[-1].rsplit(".", 1)[0]
        if fid not in self.files:
            return web.Response(status=404)
        return web.Response(body=self.files[fid])

    def push_voice(self, user_id: int, audio: bytes, duration: int = 3, kind: str = "voice") -> int:
        fid = f"f{self._next_update}"
        self.files[fid] = audio
        media = {"file_id": fid, "file_unique_id": fid, "duration": duration, "file_size": len(audio)}
        if kind == "video_note":
            media["length"] = 240
        return self.push_raw_message(user_id, {kind: media})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        app.router.add_get("/file/bot{token}/{path:.*}", self._download)
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

    def push_raw_message(self, user_id: int, extra: dict[str, Any], chat_type: str = "private") -> int:
        uid = self._next_update
        self._next_update += 1
        chat_id = user_id if chat_type == "private" else -100
        self.updates.put_nowait({"update_id": uid, "message": {
            "message_id": uid, "date": 1_700_000_000, "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"}, **extra}})
        return uid
