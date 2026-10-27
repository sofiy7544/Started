"""Обработка реплики пользователя: лимиты → история → Claude → ответ. Без зависимостей от Telegram."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from companion import texts
from companion.config import Settings
from companion.llm.client import LLMClient, LLMError, LLMErrorKind
from companion.llm.prompts import build_messages, build_system, build_user_context
from companion.persona import Persona
from companion.repositories import memory, users
from companion.repositories.users import User
from companion.services.budget import BudgetGuard, BudgetLevel
from companion.services.limits import CircuitBreaker, RateLimiter
from companion.services.memory import MemoryService, MemorySnapshot
from companion.services.sensitive import redact

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?…)](?=\s|$)")


class ReplyKind(enum.Enum):
    OK = "ok"
    SKIPPED = "skipped"  # ответа не будет: на это сообщение ответит обработчик более нового
    RATE_LIMITED = "rate_limited"
    DAILY_LIMIT = "daily_limit"
    BUDGET = "budget"
    TEMPORARY_ERROR = "temporary_error"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"


@dataclass(frozen=True)
class ChatReply:
    kind: ReplyKind
    text: str | None = None


def trim_to_sentence(text: str) -> str:
    """Обрезанный по max_tokens ответ заканчиваем на последнем целом предложении."""
    ends = list(_SENTENCE_END.finditer(text))
    if ends and ends[-1].end() > len(text) // 4:
        return text[: ends[-1].end()].rstrip()
    return text.rstrip() + "…"


class ChatService:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        llm: LLMClient,
        settings: Settings,
        persona: Persona,
        budget: BudgetGuard,
        memory_service: MemoryService,
        rate_limiter: RateLimiter,
        breaker: CircuitBreaker,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._llm = llm
        self._settings = settings
        self._persona = persona
        self._persona_prompt = persona.system_prompt()
        self._budget = budget
        self._memory = memory_service
        self._limiter = rate_limiter
        self._breaker = breaker
        self._now = now
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---- пользователи -------------------------------------------------------------------

    async def get_or_create_user(
        self, tg_user_id: int, first_name: str | None, language_code: str | None
    ) -> User:
        return await users.upsert(self._pool, tg_user_id, first_name, language_code)

    async def find_user(self, tg_user_id: int) -> User | None:
        return await users.get_by_tg_id(self._pool, tg_user_id)

    async def confirm_age(self, user: User) -> None:
        await users.confirm_age(self._pool, user.id)

    async def reset_context(self, user: User) -> None:
        async with self._user_lock(user.id):
            await users.reset_context(self._pool, user.id)
        # Прежний диалог сожмётся в резюме и факты в фоне.
        self._spawn(self._memory.maybe_summarize(user.id))

    async def memory_snapshot(self, user: User) -> MemorySnapshot:
        return await self._memory.snapshot(user.id)

    async def forget_memory(self, user: User) -> None:
        async with self._user_lock(user.id), self._pool.acquire() as conn, conn.transaction():
            await memory.lock_memory(conn, user.id)  # ждём фоновое сжатие, если оно идёт
            await memory.clear_memory(conn, user.id)
            await conn.execute(
                "UPDATE users SET context_from_message_id = 0 WHERE id = $1", user.id
            )

    async def delete_user(self, user: User) -> None:
        async with self._user_lock(user.id), self._pool.acquire() as conn, conn.transaction():
            await memory.lock_memory(conn, user.id)  # ждём фоновое сжатие, если оно идёт
            await users.delete(conn, user.id)
        self._limiter.forget(user.id)

    # ---- переписка ------------------------------------------------------------------------

    async def handle_message(self, user: User, raw_text: str) -> ChatReply:
        text = raw_text.strip()
        if not text:
            return ChatReply(ReplyKind.SKIPPED)
        allowed, warn = self._limiter.check(user.id)
        if not allowed:
            return ChatReply(ReplyKind.RATE_LIMITED, texts.TOO_FAST if warn else None)
        today = await memory.count_user_messages_today(self._pool, user.id)
        if today >= self._settings.daily_messages_per_user:
            return ChatReply(ReplyKind.DAILY_LIMIT, texts.DAILY_LIMIT)

        limit = self._settings.max_user_message_chars
        if len(text) > limit:
            text = text[:limit] + " […сообщение обрезано]"
        text = redact(text)

        message_id = await memory.add_message(self._pool, user.id, "user", text)
        async with self._user_lock(user.id):
            # Если пока мы ждали, пришло более новое сообщение, — ответим один раз на всю пачку.
            if await memory.latest_user_message_id(self._pool, user.id) != message_id:
                return ChatReply(ReplyKind.SKIPPED)
            reply = await self._generate(user.id)
        if reply.kind is ReplyKind.OK:
            self._spawn(self._memory.maybe_summarize(user.id))
        return reply

    async def _generate(self, user_id: int) -> ChatReply:
        if not self._breaker.allow():
            return ChatReply(ReplyKind.UNAVAILABLE, texts.UNAVAILABLE)
        level = await self._budget.level()
        if level is BudgetLevel.EXHAUSTED:
            return ChatReply(ReplyKind.BUDGET, texts.BUDGET_EXHAUSTED)
        model = self._settings.chat_model if level is BudgetLevel.OK else self._settings.memory_model

        user = await users.get(self._pool, user_id)
        if user is None:  # удалён, пока ждали блокировку
            return ChatReply(ReplyKind.SKIPPED)
        history = await memory.context_messages(
            self._pool, user_id, user.context_from_message_id, self._settings.max_context_messages
        )
        user_ids = [m.id for m in history if m.role == "user"]
        if not user_ids:
            return ChatReply(ReplyKind.SKIPPED)
        answering_id = max(user_ids)
        previous = await memory.last_message_before(self._pool, user_id, answering_id)
        user_context = build_user_context(
            first_name=user.first_name,
            facts=await memory.list_facts(self._pool, user_id),
            summaries=await self._memory.recent_summaries(user_id),
            history=history,
            previous_message=previous,
            now=self._now(),
        )
        system = build_system(self._persona_prompt, user_context)
        messages = build_messages(history, self._settings.max_context_chars)
        if not messages:
            return ChatReply(ReplyKind.SKIPPED)

        input_chars = sum(len(b["text"]) for b in system) + sum(len(m["content"]) for m in messages)
        reservation = await self._budget.reserve(
            model, input_chars=input_chars, max_output_tokens=self._settings.max_reply_tokens
        )
        if reservation is None:
            return ChatReply(ReplyKind.BUDGET, texts.BUDGET_EXHAUSTED)

        try:
            resp = await self._budget.paid_call(
                reservation,
                lambda: self._llm.chat(
                    model=model, system=system, messages=messages,
                    max_tokens=self._settings.max_reply_tokens,
                ),
                lambda r: r,
                operation="chat",
                user_id=user_id,
            )
        except LLMError as exc:
            self._breaker.failure(exc.kind)
            if exc.kind is LLMErrorKind.TEMPORARY:
                log.warning("llm temporary error for user=%s: %s", user_id, exc)
                return ChatReply(ReplyKind.TEMPORARY_ERROR, texts.TEMPORARY_ERROR)
            log.error("llm configuration error (check ANTHROPIC_API_KEY / model): %s", exc)
            return ChatReply(ReplyKind.UNAVAILABLE, texts.UNAVAILABLE)
        self._breaker.success()

        if resp.refused:
            return ChatReply(ReplyKind.REFUSED, texts.REFUSED)
        answer = resp.text
        if resp.truncated and answer:
            answer = trim_to_sentence(answer)
        if not answer:
            log.warning("empty model answer for user=%s (truncated=%s)", user_id, resp.truncated)
            return ChatReply(ReplyKind.TEMPORARY_ERROR, texts.TEMPORARY_ERROR)
        try:
            await memory.add_message(
                self._pool, user_id, "assistant", answer, resp.served_model,
                reply_to_id=answering_id,
            )
        except asyncpg.ForeignKeyViolationError:
            return ChatReply(ReplyKind.SKIPPED)  # пользователь удалил данные во время генерации
        return ChatReply(ReplyKind.OK, answer)

    # ---- служебное ----------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        """Сериализует работу с одним пользователем; блокировки освобождаются из словаря."""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        self._lock_users[user_id] = self._lock_users.get(user_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_users[user_id] -= 1
            if self._lock_users[user_id] == 0:
                del self._lock_users[user_id]
                del self._locks[user_id]

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    async def wait_background(self) -> None:
        """Дождаться фоновой работы с памятью (в тестах и при остановке бота)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
