"""Сценарий ответа ассистента. Не зависит от Telegram: принимает текст, возвращает текст."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.bot import texts
from pai.config import Settings
from pai.db.models import AuditEvent, Conversation, Turn, User
from pai.llm.catalog import Catalog, ModelInfo
from pai.llm.engine import ChatEngine, FailureKind, LLMFailure, load_history
from pai.services.budget import BudgetGuard
from pai.services.limits import CircuitBreaker, RateLimiter

log = logging.getLogger(__name__)

REPLY_STYLES = {
    "short": "Отвечай максимально коротко: 1–3 предложения, если не просят подробнее.",
    "normal": "",
    "detailed": "Отвечай подробно и структурированно, с примерами, если они уместны.",
}

_FAILURE_TEXTS = {
    FailureKind.AUTH: texts.ERR_AUTH,
    FailureKind.QUOTA: texts.ERR_QUOTA,
    FailureKind.RATE_LIMIT: texts.ERR_RATE_LIMIT,
    FailureKind.TEMPORARY: texts.ERR_TEMPORARY,
    FailureKind.TIMEOUT: texts.ERR_TIMEOUT,
    FailureKind.MODEL_NOT_FOUND: texts.ERR_MODEL_NOT_FOUND,
    FailureKind.CONTEXT_TOO_LONG: texts.ERR_CONTEXT_TOO_LONG,
    FailureKind.TOO_MANY_STEPS: texts.ERR_TOO_MANY_STEPS,
    FailureKind.BAD_RESPONSE: texts.ERR_BAD_RESPONSE,
    FailureKind.REFUSED: texts.ERR_REFUSED,
    FailureKind.CONFIG: texts.ERR_CONFIG,
}


class Outcome(enum.Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    BUDGET = "budget"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class Answer:
    outcome: Outcome
    text: str | None
    model: ModelInfo | None = None
    cost_usd: Decimal = Decimal(0)


class Assistant:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker,
        settings: Settings,
        catalog: Catalog,
        engine: ChatEngine,
        budget: BudgetGuard,
        rate_limiter: RateLimiter,
        breaker: CircuitBreaker,
        base_instructions: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._catalog = catalog
        self._engine = engine
        self._budget = budget
        self._limiter = rate_limiter
        self._breaker = breaker
        self._base_instructions = base_instructions.strip()
        self._now = now
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def budget(self) -> BudgetGuard:
        return self._budget

    # ---- пользователь и настройки ----------------------------------------------------

    async def get_user(self, tg_id: int, first_name: str | None = None) -> User:
        # Два одновременных сообщения нового пользователя: оба не находят запись и пытаются
        # её создать; второй получит IntegrityError — тогда просто читаем созданную.
        for _attempt in range(2):
            try:
                async with self._sessions() as s, s.begin():
                    user = await s.scalar(select(User).where(User.tg_id == tg_id))
                    if user is None:
                        user = User(tg_id=tg_id, first_name=first_name)
                        s.add(user)
                        await s.flush()
                    elif first_name and user.first_name != first_name:
                        user.first_name = first_name
                    return user
            except IntegrityError:
                continue
        raise RuntimeError("не удалось создать или прочитать пользователя")

    def current_model(self, user: User) -> ModelInfo:
        return self._catalog.resolve(user.model_id, self._settings)

    async def set_model(self, user: User, model_id: str) -> ModelInfo | None:
        model = next((m for m in self._catalog.available(self._settings) if m.id == model_id), None)
        if model is None:
            return None
        await self._update_user(user, model_id=model.id)
        await self.audit(user.tg_id, "model_changed", model.id)
        return model

    async def set_reply_style(self, user: User, style: str) -> bool:
        if style not in REPLY_STYLES:
            return False
        await self._update_user(user, reply_style=style)
        await self.audit(user.tg_id, "style_changed", style)
        return True

    async def set_instructions(self, user: User, text: str | None) -> None:
        value = text.strip()[:2000] if text and text.strip() else None
        await self._update_user(user, instructions=value)
        detail = "cleared" if value is None else f"{len(value)} chars"
        await self.audit(user.tg_id, "instructions_changed", detail)

    async def _update_user(self, user: User, **values: object) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(update(User).where(User.id == user.id).values(**values))
        for key, value in values.items():
            setattr(user, key, value)

    async def new_conversation(self, user: User) -> None:
        async with self._user_lock(user.id), self._sessions() as s, s.begin():
            await s.execute(
                update(Conversation)
                .where(Conversation.user_id == user.id, Conversation.closed_at.is_(None))
                .values(closed_at=self._now())
            )
        await self.audit(user.tg_id, "conversation_reset")

    async def audit(self, tg_id: int | None, kind: str, detail: str | None = None) -> None:
        async with self._sessions() as s, s.begin():
            s.add(AuditEvent(tg_id=tg_id, kind=kind, detail=detail))

    # ---- разговор ----------------------------------------------------------------------

    async def _open_conversation(self, s, user: User) -> Conversation:  # type: ignore[no-untyped-def]
        conv = await s.scalar(
            select(Conversation)
            .where(Conversation.user_id == user.id, Conversation.closed_at.is_(None))
            .order_by(Conversation.id.desc())
            .limit(1)
        )
        if conv is None:
            conv = Conversation(user_id=user.id)
            s.add(conv)
            await s.flush()
        return conv

    async def _history(self, s, conv: Conversation) -> tuple[list[bytes], int]:  # type: ignore[no-untyped-def]
        """Последние обмены разговора в пределах лимитов (по целым обменам — чтобы не
        разорвать пары «вызов инструмента — результат»)."""
        rows = (await s.execute(
            select(Turn.messages, func.length(Turn.user_text) + func.length(Turn.assistant_text))
            .where(Turn.conversation_id == conv.id)
            .order_by(Turn.id.desc())
            .limit(self._settings.max_history_turns)
        )).all()
        picked: list[bytes] = []
        chars = 0
        for blob, size in rows:
            if picked and chars + size > self._settings.max_history_chars:
                break
            picked.append(blob)
            chars += size
        picked.reverse()
        return picked, chars

    def _instructions(self, user: User) -> str:
        now = self._now().astimezone(ZoneInfo("Europe/Moscow"))
        parts = [self._base_instructions]
        style = REPLY_STYLES.get(user.reply_style, "")
        if style:
            parts.append(style)
        if user.instructions:
            parts.append("Личные пожелания владельца (важнее общих правил оформления):\n" + user.instructions)
        parts.append(f"Сейчас {now:%d.%m.%Y %H:%M} (Москва).")
        return "\n\n".join(parts)

    async def ask(self, user: User, raw_text: str) -> Answer:
        text = raw_text.strip()
        if not text:
            return Answer(Outcome.FAILED, None)
        allowed, warn = self._limiter.check(user.id)
        if not allowed:
            return Answer(Outcome.RATE_LIMITED, texts.TOO_FAST if warn else None)
        limit = self._settings.max_user_message_chars
        if len(text) > limit:
            text = text[:limit] + "\n[…сообщение обрезано: слишком длинное]"

        async with self._user_lock(user.id):
            if not self._breaker.allow():
                return Answer(Outcome.UNAVAILABLE, texts.ERR_PAUSED)
            model = self.current_model(user)
            instructions = self._instructions(user)
            async with self._sessions() as s, s.begin():
                conv = await self._open_conversation(s, user)
                blobs, history_chars = await self._history(s, conv)
                conv_id = conv.id
            history = load_history(blobs)

            reservation = await self._budget.reserve(
                model, input_chars=len(instructions) + history_chars + len(text),
                max_output_tokens=self._settings.max_reply_tokens,
            )
            if reservation is None:
                return Answer(Outcome.BUDGET, texts.BUDGET_EXHAUSTED if not self._budget.disabled
                              else texts.PAID_DISABLED)
            cost_limit = min(Decimal(str(self._settings.per_request_cost_limit_usd)),
                             max(reservation.estimate * 3, Decimal("0.0001")))
            try:
                reply = await self._budget.paid_call(
                    reservation,
                    lambda: self._engine.reply(
                        model, prompt=text, history=history, instructions=instructions,
                        max_tokens=self._settings.max_reply_tokens, cost_limit_usd=cost_limit,
                    ),
                    user_id=user.id, operation="chat",
                )
            except LLMFailure as exc:
                self._breaker.failure(exc.kind)
                level = logging.ERROR if exc.kind in (FailureKind.AUTH, FailureKind.CONFIG,
                                                      FailureKind.QUOTA) else logging.WARNING
                log.log(level, "llm failure user=%s model=%s: %s", user.id, model.id, exc)
                await self.audit(user.tg_id, "llm_error", f"{model.id}: {exc.kind.value}")
                return Answer(Outcome.FAILED, _FAILURE_TEXTS[exc.kind], model)
            self._breaker.success()

            answer = reply.text or texts.EMPTY_ANSWER
            async with self._sessions() as s, s.begin():
                s.add(Turn(
                    conversation_id=conv_id, user_id=user.id, user_text=text, assistant_text=answer,
                    messages=reply.new_messages, model_id=model.id, input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens, cost_usd=reply.cost_usd,
                ))
            return Answer(Outcome.OK, answer, model, reply.cost_usd)

    @contextlib.asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            yield
