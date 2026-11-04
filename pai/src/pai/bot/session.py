"""Сессия aiogram, которая уважает стандартные HTTPS_PROXY и SSL_CERT_FILE.

По умолчанию aiogram (aiohttp) игнорирует системный прокси и доверяет только встроенному
набору сертификатов. Клиенты OpenAI/Anthropic эти переменные учитывают — делаем так же,
чтобы бот работал в сети с прокси (корпоративной или облачной). Без переменных поведение
не меняется.
"""

from __future__ import annotations

import os
import ssl
from typing import Any

import certifi
from aiogram.__meta__ import __version__ as aiogram_version
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientSession
from aiohttp.hdrs import USER_AGENT


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    extra = os.environ.get("SSL_CERT_FILE")
    if extra and os.path.isfile(extra):
        ctx.load_verify_locations(cafile=extra)
    return ctx


class EnvAwareSession(AiohttpSession):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connector_init["ssl"] = _ssl_context()

    async def create_session(self) -> ClientSession:
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={USER_AGENT: f"aiogram/{aiogram_version}"},
                trust_env=True,  # HTTPS_PROXY / NO_PROXY из окружения
            )
            self._should_reset_connector = False
        return self._session
