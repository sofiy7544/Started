"""Цикл доставки напоминаний: раз в несколько секунд забирает наступившие и отправляет в Telegram.
Работает отдельно от инициативных сообщений — долгая генерация там не задержит напоминание."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import User
from pai.reminders.service import Due, ReminderService

log = logging.getLogger(__name__)
TICK_SECONDS = 15


class DeliveryError(RuntimeError):
    """permanent — повторять бессмысленно (бот заблокирован, чат удалён)."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


class ReminderSender(Protocol):
    async def send_reminder(self, tg_id: int, due: Due, text: str) -> int | None: ...


def reminder_text(due: Due, late_note: str) -> str:
    text = f"⏰ Напоминание: {due.reminder.text}"
    return f"{text}\n{late_note}" if due.late else text


class ReminderDelivery:
    def __init__(
        self,
        *,
        service: ReminderService,
        sessions: async_sessionmaker,
        sender: ReminderSender,
        on_sent=None,
        late_note: str = "",
    ) -> None:  # type: ignore[no-untyped-def]
        self._service = service
        self._sessions = sessions
        self._sender = sender
        self._on_sent = on_sent  # (user_id, note, text) → сохранить в истории разговора
        self._late_note = late_note

    async def tick(self) -> int:
        sent = 0
        for due in await self._service.claim_due():
            async with self._sessions() as s:
                tg_id = await s.scalar(select(User.tg_id).where(User.id == due.reminder.user_id))
            if tg_id is None:
                continue
            text = reminder_text(due, self._late_note)
            try:
                message_id = await self._sender.send_reminder(tg_id, due, text)
            except DeliveryError as exc:
                await self._service.mark_failed(due, str(exc), permanent=exc.permanent)
                log.warning(
                    "reminder #%s not delivered (%s): %s",
                    due.reminder.id,
                    "permanent" if exc.permanent else "will retry",
                    exc,
                )
                continue
            await self._service.mark_sent(due, message_id)
            log.info(
                "reminder #%s sent to user=%s (scheduled %s, late=%s)",
                due.reminder.id,
                due.reminder.user_id,
                due.scheduled_for.isoformat(),
                due.late,
            )
            if self._on_sent is not None:
                await self._on_sent(due.reminder.user_id, f"сработало напоминание #{due.reminder.id}", text)
            sent += 1
        return sent

    async def run_forever(self, interval: float = TICK_SECONDS) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — цикл доставки не должен останавливаться
                log.error("reminder delivery tick failed", exc_info=True)
            await asyncio.sleep(interval)
