"""Настройка логов без персональных данных.

Сообщения исключений могут содержать пользовательский текст: asyncpg пишет значения
строк в DETAIL («Key (user_id, fact_norm)=(5, любит джаз)»), pydantic — input_value.
Поэтому в трассировках выводятся только типы исключений и места в коде, без сообщений.
"""

from __future__ import annotations

import logging
import traceback
from types import TracebackType

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Базовый Formatter переиспользует record.exc_text, если запись уже отформатировал
        # другой обработчик, — там может быть полный текст исключения. Не доверяем кэшу.
        cached = record.exc_text
        record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_text = cached

    def formatException(self, ei: _ExcInfo) -> str:  # noqa: N802 — имя из logging.Formatter
        lines: list[str] = []
        exc: BaseException | None = ei[1]
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            for frame in traceback.extract_tb(exc.__traceback__):
                lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
            lines.append(f"{type(exc).__module__}.{type(exc).__qualname__} (текст исключения скрыт)")
            exc = exc.__cause__ or exc.__context__
        return "\n".join(lines)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # На уровне INFO HTTP-клиенты логируют URL запросов — в URL Bot API есть токен бота.
    for noisy in ("httpx", "httpx2", "aiohttp.access", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
