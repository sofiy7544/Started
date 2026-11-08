"""Задачи: добавить, показать, изменить, отметить выполненной. Всё ограничено user_id."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import Task

MAX_OPEN = 500
STATUSES = ("open", "done", "cancelled")


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class TaskView:
    id: int
    section: str
    text: str
    person: str | None
    due: str | None
    status: str

    def line(self) -> str:
        mark = {"open": "☐", "done": "✅", "cancelled": "✖"}.get(self.status, "•")
        extra = [x for x in (self.person, self.due) if x]
        return f"{mark} #{self.id} {self.text}" + (f" ({'; '.join(extra)})" if extra else "")


def _view(t: Task) -> TaskView:
    return TaskView(t.id, t.section, t.text, t.person, t.due, t.status)


def _clean(value: str | None, limit: int) -> str | None:
    value = " ".join((value or "").split())
    return value[:limit] or None


class TaskService:
    def __init__(self, sessions: async_sessionmaker, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._sessions = sessions
        self._now = now

    async def add(self, user_id: int, text: str, *, section: str | None = None, person: str | None = None,
                  due: str | None = None) -> TaskView:
        body = (text or "").strip()
        if not body:
            raise TaskError("пустая задача")
        async with self._sessions() as s, s.begin():
            count = len((await s.scalars(select(Task.id).where(Task.user_id == user_id, Task.status == "open"))).all())
            if count >= MAX_OPEN:
                raise TaskError(f"открытых задач уже {MAX_OPEN}")
            t = Task(user_id=user_id, section=_clean(section, 100) or "Общее", text=body[:2000],
                     person=_clean(person, 100), due=_clean(due, 50), status="open", created_at=self._now())
            s.add(t)
            await s.flush()
            return _view(t)

    async def list(self, user_id: int, *, include_closed: bool = False, section: str | None = None) -> list[TaskView]:
        q = select(Task).where(Task.user_id == user_id)
        if not include_closed:
            q = q.where(Task.status == "open")
        if section:
            q = q.where(Task.section == section)
        async with self._sessions() as s:
            return [_view(t) for t in await s.scalars(q.order_by(Task.section, Task.id))]

    async def update(self, user_id: int, task_id: int, **values: str | None) -> TaskView:
        async with self._sessions() as s, s.begin():
            t = await s.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
            if t is None:
                raise TaskError(f"задачи #{task_id} нет")
            if values.get("text") is not None:
                if not values["text"].strip():
                    raise TaskError("пустой текст")
                t.text = values["text"].strip()[:2000]
            if values.get("section") is not None:
                t.section = _clean(values["section"], 100) or "Общее"
            for key, limit in (("person", 100), ("due", 50)):
                if values.get(key) is not None:
                    setattr(t, key, _clean(values[key], limit))
            status = values.get("status")
            if status is not None:
                if status not in STATUSES:
                    raise TaskError("статус: open, done или cancelled")
                t.status = status
                t.done_at = self._now() if status != "open" else None
            return _view(t)


def grouped_text(items: list[TaskView]) -> str:
    """Задачи по разделам — для сообщения в Telegram."""
    out: list[str] = []
    section = None
    for v in items:
        if v.section != section:
            section = v.section
            out.append(f"\n📌 {section}")
        out.append(v.line())
    return "\n".join(out).strip()


def to_csv_table(items: list[TaskView]) -> str:
    """Содержимое для выгрузки в Excel (outputs.build_file)."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Раздел", "Задача", "Кто / с кем", "Срок", "Статус"])
    titles = {"open": "в работе", "done": "сделано", "cancelled": "отменено"}
    for v in items:
        w.writerow([v.id, v.section, v.text, v.person or "", v.due or "", titles.get(v.status, v.status)])
    return buf.getvalue()
