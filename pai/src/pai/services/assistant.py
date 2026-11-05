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

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.bot import texts
from pai.config import Settings
from pai.db.models import AuditEvent, Conversation, Turn, User
from pai.llm.catalog import Catalog, ModelInfo
from pai.llm.engine import ChatEngine, FailureKind, LLMFailure, load_history
from pai.memory.engine import MemoryEngine
from pai.reminders.schedule import WEEKDAYS_NOM, calendar_hint, zone
from pai.reminders.service import ReminderService
from pai.reminders.tools import INSTRUCTIONS as REMINDER_INSTRUCTIONS
from pai.reminders.tools import ReminderEffects, build_toolset
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
    reminders: ReminderEffects | None = None  # что реально сделали инструменты напоминаний


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
        memory: MemoryEngine | None = None,
        reminders: ReminderService | None = None,
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
        self.memory = memory
        self.reminders = reminders
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

    async def set_voice(self, user: User, **values: object) -> None:
        """Настройки голоса (значения проверяет обработчик по спискам движка)."""
        allowed = {"voice_mode", "tts_voice", "tts_speed", "tts_style"}
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            await self._update_user(user, **values)
            await self.audit(user.tg_id, "voice_settings_changed", ",".join(sorted(values)))

    async def set_schedule_prefs(self, user: User, **values: object) -> None:
        """Инициатива, частота, тихие часы, часовой пояс (значения проверяет обработчик)."""
        allowed = {"proactive_enabled", "proactive_frequency", "quiet_start", "quiet_end", "timezone"}
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            await self._update_user(user, **values)
            await self.audit(user.tg_id, "schedule_settings_changed", ",".join(sorted(values)))

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

    async def record_outgoing(self, user_id: int, note: str, text: str) -> None:
        """Сообщение, которое бот отправил сам (напоминание, инициатива), — в историю разговора,
        чтобы ответ владельца («да, сделал») был понятен модели. user_text пустой — служебная запись."""
        messages = ModelMessagesTypeAdapter.dump_json(
            [
                ModelRequest(parts=[UserPromptPart(content=f"(служебно: {note})")]),
                ModelResponse(parts=[TextPart(content=text)]),
            ]
        )
        async with self._user_lock(user_id), self._sessions() as s, s.begin():
            user = await s.scalar(select(User).where(User.id == user_id))
            if user is None:
                return
            conv = await self._open_conversation(s, user)
            s.add(
                Turn(
                    conversation_id=conv.id,
                    user_id=user_id,
                    user_text="",
                    assistant_text=text,
                    messages=messages,
                    model_id="system",
                    memory_processed=True,
                    created_at=self._now(),
                )
            )

    async def last_user_activity(self, user_id: int) -> datetime | None:
        async with self._sessions() as s:
            value = await s.scalar(
                select(func.max(Turn.created_at)).where(Turn.user_id == user_id, Turn.user_text != "")
            )
        return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)

    async def new_conversation(self, user: User) -> None:
        async with self._user_lock(user.id), self._sessions() as s, s.begin():
            await s.execute(
                update(Conversation)
                .where(Conversation.user_id == user.id, Conversation.closed_at.is_(None))
                .values(closed_at=self._now())
            )
        await self.audit(user.tg_id, "conversation_reset")
        if self.memory is not None:
            self.memory.schedule(user.id, force=True)  # дообработать закрытый разговор

    async def remember(self, user: User, text: str) -> bool:
        """Явная просьба запомнить (/remember): достоверно и важно."""
        if self.memory is None:
            return False
        saved = await self.memory.store.add(
            user.id, "fact", text, confidence=1.0, importance=3, source="user"
        )
        await self.audit(user.tg_id, "memory_added_by_user")
        return saved is not None

    async def clear_memory(self, user: User) -> None:
        if self.memory is None:
            return
        async with self.memory.lock(user.id):
            await self.memory.store.clear(user.id)
        await self.audit(user.tg_id, "memory_cleared")

    async def wipe_user(self, user: User) -> None:
        """Полное удаление личных данных: память, переписка, настройки, журнал действий.
        Учёт расходов остаётся обезличенным (user_id = NULL)."""
        lock = self.memory.lock(user.id) if self.memory else contextlib.nullcontext()
        async with self._user_lock(user.id), lock, self._sessions() as s, s.begin():
            if self.memory is not None:
                await self.memory.store.clear(user.id)
            await s.execute(delete(AuditEvent).where(AuditEvent.tg_id == user.tg_id))
            await s.execute(delete(User).where(User.id == user.id))  # turns, conversations — каскадом
        log.info("user data wiped: user=%s", user.id)

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
        rows = (
            await s.execute(
                select(Turn.messages, func.length(Turn.user_text) + func.length(Turn.assistant_text))
                .where(Turn.conversation_id == conv.id)
                .order_by(Turn.id.desc())
                .limit(self._settings.max_history_turns)
            )
        ).all()
        picked: list[bytes] = []
        chars = 0
        for blob, size in rows:
            if picked and chars + size > self._settings.max_history_chars:
                break
            picked.append(blob)
            chars += size
        picked.reverse()
        return picked, chars

    def _instructions(self, user: User, memory_block: str = "") -> str:
        tz = zone(user.timezone)
        now = self._now().astimezone(tz)
        parts = [self._base_instructions]
        if memory_block:
            parts.append(memory_block)
        style = REPLY_STYLES.get(user.reply_style, "")
        if style:
            parts.append(style)
        if user.instructions:
            parts.append("Личные пожелания владельца (важнее общих правил оформления):\n" + user.instructions)
        parts.append(
            f"Сейчас {now:%d.%m.%Y %H:%M}, {WEEKDAYS_NOM[now.weekday()]}; часовой пояс владельца — "
            f"{user.timezone}. Ближайшие дни: {calendar_hint(self._now(), tz)}."
        )
        if self.reminders is not None:
            parts.append(REMINDER_INSTRUCTIONS)
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
            memory_block = await self.memory.context_block(user.id, text) if self.memory else ""
            instructions = self._instructions(user, memory_block)
            async with self._sessions() as s, s.begin():
                conv = await self._open_conversation(s, user)
                blobs, history_chars = await self._history(s, conv)
                conv_id = conv.id
            history = load_history(blobs)

            reservation = await self._budget.reserve(
                model,
                input_chars=len(instructions) + history_chars + len(text),
                max_output_tokens=self._settings.max_reply_tokens,
            )
            if reservation is None:
                return Answer(
                    Outcome.BUDGET,
                    texts.BUDGET_EXHAUSTED if not self._budget.disabled else texts.PAID_DISABLED,
                )
            cost_limit = min(
                Decimal(str(self._settings.per_request_cost_limit_usd)),
                max(reservation.estimate * 3, Decimal("0.0001")),
            )
            effects = ReminderEffects()
            toolsets = [build_toolset(self.reminders, user, effects)] if self.reminders else None
            try:
                reply = await self._budget.paid_call(
                    reservation,
                    lambda: self._engine.reply(
                        model,
                        prompt=text,
                        history=history,
                        instructions=instructions,
                        max_tokens=self._settings.max_reply_tokens,
                        cost_limit_usd=cost_limit,
                        toolsets=toolsets,
                    ),
                    user_id=user.id,
                    operation="chat",
                )
            except LLMFailure as exc:
                self._breaker.failure(exc.kind)
                level = (
                    logging.ERROR
                    if exc.kind in (FailureKind.AUTH, FailureKind.CONFIG, FailureKind.QUOTA)
                    else logging.WARNING
                )
                log.log(level, "llm failure user=%s model=%s: %s", user.id, model.id, exc)
                await self.audit(user.tg_id, "llm_error", f"{model.id}: {exc.kind.value}")
                # Инструменты могли успеть сработать до ошибки — карточки всё равно покажем.
                return Answer(
                    Outcome.FAILED,
                    _FAILURE_TEXTS[exc.kind],
                    model,
                    reminders=effects if effects.any else None,
                )
            self._breaker.success()

            answer = reply.text or texts.EMPTY_ANSWER
            async with self._sessions() as s, s.begin():
                s.add(
                    Turn(
                        conversation_id=conv_id,
                        user_id=user.id,
                        user_text=text,
                        assistant_text=answer,
                        messages=reply.new_messages,
                        model_id=model.id,
                        input_tokens=reply.input_tokens,
                        output_tokens=reply.output_tokens,
                        cost_usd=reply.cost_usd,
                        created_at=self._now(),
                    )
                )
        if self.memory is not None:
            self.memory.schedule(user.id)
        return Answer(Outcome.OK, answer, model, reply.cost_usd, reminders=effects if effects.any else None)

    @contextlib.asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            yield
