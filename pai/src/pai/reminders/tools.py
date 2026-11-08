"""Инструменты модели для напоминаний. Создаются на каждый запрос и привязаны к одному владельцу:
модель не может передать чужой user_id. Ошибки возвращаются модели текстом, чтобы она переспросила."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import FunctionToolset

from pai.db.models import User
from pai.reminders.service import ReminderError, ReminderService, ReminderView

Repeat = Literal["none", "daily", "weekdays", "weekly", "monthly"]


@dataclass
class ReminderEffects:
    """Что инструменты реально сделали за этот ответ — из этого бот строит карточки подтверждения."""

    created: list[ReminderView] = field(default_factory=list)
    updated: list[ReminderView] = field(default_factory=list)
    deleted: list[ReminderView] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.created or self.updated or self.deleted)


def build_toolset(service: ReminderService, user: User, effects: ReminderEffects) -> FunctionToolset:
    async def create_reminder(text: str, at_local: str | None = None, in_minutes: int | None = None,
                              repeat: Repeat = "none") -> str:
        """Создать напоминание владельцу. Укажи РОВНО одно: at_local или in_minutes.

        Args:
            text: о чём напомнить, коротко и по сути («позвонить врачу»)
            at_local: дата и время в местном часовом поясе владельца, формат YYYY-MM-DD HH:MM
            in_minutes: через сколько минут от текущего момента («через 2 часа» = 120)
            repeat: повтор: none — разово, daily — каждый день, weekdays — по будням,
                weekly — каждую неделю в тот же день недели, monthly — каждый месяц в то же число
        """
        try:
            view = await service.create(user, text, at_local=at_local, in_minutes=in_minutes, repeat=repeat)
        except ReminderError as exc:
            return f"Не создано: {exc}"
        effects.created.append(view)
        return f"Создано {view.line()}"

    async def list_reminders() -> str:
        """Показать активные напоминания владельца (с номерами для изменения и удаления)."""
        items = await service.list_active(user.id)
        return "\n".join(v.line() for v in items) if items else "Активных напоминаний нет."

    async def update_reminder(reminder_id: int, text: str | None = None, at_local: str | None = None,
                              in_minutes: int | None = None, repeat: Repeat | None = None) -> str:
        """Изменить напоминание: текст, время (at_local или in_minutes) или повтор.

        Args:
            reminder_id: номер напоминания (из list_reminders)
            text: новый текст, если меняется
            at_local: новое время YYYY-MM-DD HH:MM, если меняется
            in_minutes: новое время «через N минут», если так удобнее
            repeat: новый повтор, если меняется
        """
        try:
            view = await service.update(user, reminder_id, text=text, at_local=at_local,
                                        in_minutes=in_minutes, repeat=repeat)
        except ReminderError as exc:
            return f"Не изменено: {exc}"
        effects.updated.append(view)
        return f"Изменено {view.line()}"

    async def delete_reminder(reminder_id: int) -> str:
        """Удалить (отменить) напоминание по номеру.

        Args:
            reminder_id: номер напоминания (из list_reminders)
        """
        try:
            view = await service.cancel(user.id, reminder_id)
        except ReminderError as exc:
            return f"Не удалено: {exc}"
        effects.deleted.append(view)
        return f"Удалено {view.line()}"

    return FunctionToolset([create_reminder, list_reminders, update_reminder, delete_reminder])


INSTRUCTIONS = """\
## Напоминания
У тебя есть инструменты для напоминаний: create_reminder, list_reminders, update_reminder, delete_reminder.
- Когда владелец просит напомнить, перенести или отменить напоминание (текстом или голосом) — вызови инструмент.
- Время указывай в часовом поясе владельца. «Через 2 часа» — in_minutes=120. «Завтра в 10» — at_local.
- Если не ясно, когда напомнить (нет времени: «в пятницу напомни про встречу»), — НЕ угадывай:
  спроси, во сколько, и создай напоминание после ответа.
- «Каждый понедельник…» — repeat=weekly с первой датой в ближайший понедельник; «каждый день» — daily;
  «по будням» — weekdays; «каждый месяц 5-го» — monthly.
- Чтобы изменить или удалить, сначала узнай номер через list_reminders, если его нет в разговоре.
- Прежде чем говорить, какие напоминания стоят, — вызови list_reminders: старые из переписки могли уже сработать.
- Никогда не говори, что напоминание создано, изменено или удалено, если инструмент не ответил «Создано»,
  «Изменено» или «Удалено». Если инструмент вернул «Не …» — объясни владельцу и уточни.
- Подтверждение с номером и временем бот покажет сам — не пересказывай его дословно, ответь коротко."""
