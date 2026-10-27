"""Ограничение частоты сообщений и предохранитель от лавины запросов к упавшему API."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from companion.llm.client import LLMErrorKind

Clock = Callable[[], float]


class RateLimiter:
    """Не больше `per_minute` сообщений пользователя за скользящие 60 секунд (в памяти процесса)."""

    def __init__(self, per_minute: int, clock: Clock = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[int, deque[float]] = {}
        self._warned_at: dict[int, float] = {}

    def check(self, user_id: int) -> tuple[bool, bool]:
        """Возвращает (разрешено, нужно_предупредить). Предупреждаем один раз за окно."""
        now = self._clock()
        hits = self._hits.setdefault(user_id, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) < self._limit:
            hits.append(now)
            return True, False
        warned = self._warned_at.get(user_id)
        if warned is None or now - warned >= 60:
            self._warned_at[user_id] = now
            return False, True
        return False, False

    def forget(self, user_id: int) -> None:
        self._hits.pop(user_id, None)
        self._warned_at.pop(user_id, None)


class CircuitBreaker:
    """После N ошибок подряд временно не обращаемся к API, чтобы не копить запросы и расходы.

    Ошибка ключа (AUTH) сама не исправится — пауза в 10 раз длиннее.
    """

    def __init__(self, threshold: int, cooldown: float, clock: Clock = time.monotonic) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self._open_until

    def success(self) -> None:
        self._failures = 0

    def failure(self, kind: LLMErrorKind) -> None:
        self._failures += 1
        if kind is LLMErrorKind.AUTH:
            self._open_until = self._clock() + self._cooldown * 10
        elif self._failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown
            self._failures = 0
