"""Напоминания: создание, изменение, удаление и надёжная доставка без дублей.
Все операции владельца ограничены user_id — чужое напоминание не найти и не изменить."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import REPEAT_KINDS, Reminder, ReminderDelivery, User
from pai.reminders.schedule import ScheduleError, human_repeat, human_when, next_after, parse_local, zone

MAX_TEXT = 300
MAX_ACTIVE_PER_USER = 100
MAX_AHEAD_DAYS = 3 * 365
MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=2)  # «отправляется» дольше — процесс упал посреди отправки
LATE_AFTER = timedelta(minutes=5)  # опоздание больше — помечаем, что бот был выключен


class ReminderError(ValueError):
    """Ошибка в запросе на напоминание — понятный текст для модели и пользователя."""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ReminderView:
    id: int
    text: str
    when: str  # «завтра в 10:00»
    repeat: str  # «каждый понедельник в 09:00» или ""
    next_run_at: datetime

    def line(self) -> str:
        rep = f" ({self.repeat})" if self.repeat else ""
        return f"#{self.id} — {self.when}{rep}: {self.text}"


@dataclass(frozen=True)
class Due:
    reminder: Reminder
    delivery_id: int
    scheduled_for: datetime
    late: bool


class ReminderService:
    def __init__(self, sessions: async_sessionmaker, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._sessions = sessions
        self._now = now

    # ---- представление -------------------------------------------------------------------

    def view(self, r: Reminder) -> ReminderView:
        tz = zone(r.timezone)
        when = _aware(r.next_run_at)
        rep = human_repeat(r.repeat, r.local_time, when.astimezone(tz).weekday(), r.anchor_day)
        return ReminderView(r.id, r.text, human_when(when, tz, self._now()), rep, when)

    # ---- запись --------------------------------------------------------------------------

    def _resolve(self, user: User, at_local: str | None, in_minutes: int | None) -> datetime:
        tz = zone(user.timezone)
        now = self._now()
        if in_minutes is not None:
            if in_minutes < 1:
                raise ReminderError("через сколько минут — нужно положительное число")
            when = now + timedelta(minutes=in_minutes)
        elif at_local:
            try:
                when = parse_local(at_local, tz)
            except ScheduleError as exc:
                raise ReminderError(str(exc)) from exc
        else:
            raise ReminderError("не указано время: спроси у владельца, когда напомнить")
        when = when.replace(second=0, microsecond=0)
        if when <= now:
            raise ReminderError("это время уже прошло — уточни у владельца дату и время")
        if when > now + timedelta(days=MAX_AHEAD_DAYS):
            raise ReminderError("слишком далеко в будущем (больше трёх лет)")
        return when.astimezone(UTC)

    async def create(
        self,
        user: User,
        text: str,
        *,
        at_local: str | None = None,
        in_minutes: int | None = None,
        repeat: str = "none",
    ) -> ReminderView:
        text = " ".join(text.split())
        if not text:
            raise ReminderError("не указано, о чём напомнить")
        if len(text) > MAX_TEXT:
            raise ReminderError(f"текст напоминания длиннее {MAX_TEXT} символов")
        if repeat not in REPEAT_KINDS:
            raise ReminderError(f"повтор может быть: {', '.join(REPEAT_KINDS)}")
        when = self._resolve(user, at_local, in_minutes)
        local = when.astimezone(zone(user.timezone))
        async with self._sessions() as s, s.begin():
            active = (
                await s.execute(
                    select(Reminder.id).where(Reminder.user_id == user.id, Reminder.status == "active")
                )
            ).all()
            if len(active) >= MAX_ACTIVE_PER_USER:
                raise ReminderError(f"уже {MAX_ACTIVE_PER_USER} активных напоминаний — удалите ненужные")
            r = Reminder(
                user_id=user.id,
                text=text,
                next_run_at=when,
                local_time=f"{local:%H:%M}",
                repeat=repeat,
                anchor_day=local.day if repeat == "monthly" else None,
                timezone=user.timezone,
                created_at=self._now(),
                updated_at=self._now(),
            )
            s.add(r)
            await s.flush()
            return self.view(r)

    async def list_active(self, user_id: int) -> list[ReminderView]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Reminder)
                .where(Reminder.user_id == user_id, Reminder.status == "active")
                .order_by(Reminder.next_run_at)
            )
            return [self.view(r) for r in rows]

    async def _own(self, s, user_id: int, reminder_id: int) -> Reminder:  # type: ignore[no-untyped-def]
        r = await s.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id))
        if r is None or r.status != "active":
            raise ReminderError(f"активного напоминания #{reminder_id} нет")
        return r

    async def update(
        self,
        user: User,
        reminder_id: int,
        *,
        text: str | None = None,
        at_local: str | None = None,
        in_minutes: int | None = None,
        repeat: str | None = None,
    ) -> ReminderView:
        async with self._sessions() as s, s.begin():
            r = await self._own(s, user.id, reminder_id)
            if text is not None:
                value = " ".join(text.split())
                if not value or len(value) > MAX_TEXT:
                    raise ReminderError("пустой или слишком длинный текст")
                r.text = value
            if repeat is not None:
                if repeat not in REPEAT_KINDS:
                    raise ReminderError(f"повтор может быть: {', '.join(REPEAT_KINDS)}")
                r.repeat = repeat
            if at_local is not None or in_minutes is not None:
                when = self._resolve(user, at_local, in_minutes)
                local = when.astimezone(zone(r.timezone))
                r.next_run_at, r.local_time = when, f"{local:%H:%M}"
                r.anchor_day = local.day
            if r.repeat != "monthly":
                r.anchor_day = None
            elif r.anchor_day is None:
                r.anchor_day = _aware(r.next_run_at).astimezone(zone(r.timezone)).day
            r.updated_at = self._now()
            return self.view(r)

    async def cancel(self, user_id: int, reminder_id: int) -> ReminderView:
        async with self._sessions() as s, s.begin():
            r = await self._own(s, user_id, reminder_id)
            r.status, r.updated_at = "cancelled", self._now()
            return self.view(r)

    async def snooze(self, user_id: int, reminder_id: int, minutes: int) -> ReminderView:
        """Отложить: для разового — перенести; для повторяющегося — добавить разовое на позже."""
        async with self._sessions() as s, s.begin():
            r = await s.scalar(
                select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
            )
            if r is None or r.status == "cancelled":
                raise ReminderError(f"напоминания #{reminder_id} нет")
            when = (self._now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
            local = when.astimezone(zone(r.timezone))
            if r.repeat == "none":
                r.next_run_at, r.local_time, r.status = when, f"{local:%H:%M}", "active"
                r.updated_at = self._now()
                return self.view(r)
            copy = Reminder(
                user_id=user_id,
                text=r.text,
                next_run_at=when,
                local_time=f"{local:%H:%M}",
                repeat="none",
                timezone=r.timezone,
                created_at=self._now(),
                updated_at=self._now(),
            )
            s.add(copy)
            await s.flush()
            return self.view(copy)

    async def mark_done(self, user_id: int, reminder_id: int) -> None:
        """«✅ Готово»: разовое закрывается; у повторяющегося просто отмечается выполнение."""
        async with self._sessions() as s, s.begin():
            r = await s.scalar(
                select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
            )
            if r is not None and r.repeat == "none" and r.status == "active":
                r.status = "done"

    # ---- доставка ------------------------------------------------------------------------

    async def claim_due(self, limit: int = 50) -> list[Due]:
        """Забирает наступившие напоминания. Запись в журнале создаётся ДО отправки и уникальна
        для (напоминание, момент): даже два параллельных цикла не отправят одно срабатывание дважды."""
        now = self._now()
        async with self._sessions() as s:
            due = list(
                await s.scalars(
                    select(Reminder)
                    .where(Reminder.status == "active", Reminder.next_run_at <= now)
                    .order_by(Reminder.next_run_at)
                    .limit(limit)
                )
            )
        claimed: list[Due] = []
        for r in due:
            scheduled = _aware(r.next_run_at)
            try:
                async with self._sessions() as s, s.begin():
                    d = ReminderDelivery(
                        reminder_id=r.id, user_id=r.user_id, scheduled_for=scheduled, created_at=now
                    )
                    s.add(d)
                    await s.flush()
                    claimed.append(Due(r, d.id, scheduled, now - scheduled > LATE_AFTER))
            except IntegrityError:
                claimed += await self._retry_if_failed(r, scheduled, now)
        return claimed

    async def _retry_if_failed(self, r: Reminder, scheduled: datetime, now: datetime) -> list[Due]:
        async with self._sessions() as s, s.begin():
            d = await s.scalar(
                select(ReminderDelivery).where(
                    ReminderDelivery.reminder_id == r.id, ReminderDelivery.scheduled_for == scheduled
                )
            )
            if d is None or d.status == "sent":
                return []
            stuck = d.status == "sending" and now - _aware(d.created_at) > STUCK_AFTER
            if (d.status == "failed" or stuck) and d.attempts < MAX_ATTEMPTS:
                d.status, d.attempts, d.created_at = "sending", d.attempts + 1, now
                return [Due(r, d.id, scheduled, now - scheduled > LATE_AFTER)]
            if d.attempts >= MAX_ATTEMPTS and r.status == "active":
                await self._advance(s, r)  # не удаётся доставить — не застреваем навсегда
            return []

    async def _advance(self, s, r: Reminder) -> None:  # type: ignore[no-untyped-def]
        nxt = next_after(
            _aware(r.next_run_at),
            repeat=r.repeat,
            local_time=r.local_time,
            tz=zone(r.timezone),
            anchor_day=r.anchor_day,
            now_utc=self._now(),
        )
        await s.execute(
            update(Reminder)
            .where(Reminder.id == r.id)
            .values(
                next_run_at=nxt or r.next_run_at,
                status="active" if nxt else "done",
                sent_count=Reminder.sent_count + 1,
                updated_at=self._now(),
            )
        )

    async def mark_sent(self, due: Due, tg_message_id: int | None) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(
                update(ReminderDelivery)
                .where(ReminderDelivery.id == due.delivery_id)
                .values(status="sent", tg_message_id=tg_message_id, sent_at=self._now(), error=None)
            )
            r = await s.scalar(select(Reminder).where(Reminder.id == due.reminder.id))
            if r is not None and r.status == "active" and _aware(r.next_run_at) == due.scheduled_for:
                await self._advance(s, r)

    async def mark_failed(self, due: Due, error: str, *, permanent: bool) -> None:
        async with self._sessions() as s, s.begin():
            values = {"status": "failed", "error": error[:200]}
            if permanent:
                values["attempts"] = MAX_ATTEMPTS
            await s.execute(
                update(ReminderDelivery).where(ReminderDelivery.id == due.delivery_id).values(**values)
            )
            if permanent:
                r = await s.scalar(select(Reminder).where(Reminder.id == due.reminder.id))
                if r is not None and r.status == "active":
                    await self._advance(s, r)
