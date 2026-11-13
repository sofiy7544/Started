"""Данные контроля обучения: участники, записи, оценки, отсутствия, причины, журнал проверок, статистика.
Работает только со своими таблицами training_* — к личным данным владельца доступа нет."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import (
    TrainingAbsence,
    TrainingJob,
    TrainingPerson,
    TrainingReason,
    TrainingRecord,
    TrainingSetting,
)
from pai.training.config import Schedule, TrainingConfig
from pai.training.parser import Caption, parse_when

SCORE_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:/|из)\s*10|(?:оценка|бал+|балів|score)\s*[:\-]?\s*(\d{1,2}(?:[.,]\d)?)", re.I)


def parse_score(text: str) -> float | None:
    m = SCORE_RE.search(text or "")
    if not m:
        return None
    value = float((m.group(1) or m.group(2)).replace(",", "."))
    return value if 0 <= value <= 10 else None


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class PersonStats:
    person: TrainingPerson
    total: int = 0
    by_result: dict[str, int] = field(default_factory=dict)
    unsigned: int = 0
    scores: list[float] = field(default_factory=list)
    off: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def closed_share(self) -> float | None:
        signed = self.total - self.unsigned
        return (self.by_result.get("closed", 0) / signed) if signed else None

    @property
    def avg_score(self) -> float | None:
        return sum(self.scores) / len(self.scores) if self.scores else None


class TrainingService:
    def __init__(self, sessions: async_sessionmaker, config: TrainingConfig,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._sessions = sessions
        self.config = config
        self.now = now

    @property
    def tz(self):  # type: ignore[no-untyped-def]
        return self.config.tz

    def local_now(self) -> datetime:
        return self.now().astimezone(self.tz)

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), tzinfo=self.tz)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    # ---- настройки -----------------------------------------------------------------------

    async def _get_setting(self, key: str) -> str | None:
        async with self._sessions() as s:
            row = await s.get(TrainingSetting, key)
            return row.value if row else None

    async def _set_setting(self, key: str, value: str) -> None:
        async with self._sessions() as s, s.begin():
            row = await s.get(TrainingSetting, key)
            if row is None:
                s.add(TrainingSetting(key=key, value=value))
            else:
                row.value = value

    async def group_id(self) -> int | None:
        v = await self._get_setting("group_chat_id")
        return int(v) if v else None

    async def set_group(self, chat_id: int) -> None:
        await self._set_setting("group_chat_id", str(chat_id))

    async def silent(self) -> bool:
        """Тихий режим (по умолчанию включён): бот ничего не пишет в группу и сотрудникам,
        только молча записывает. Включается/выключается владельцем: /silent on|off."""
        return (await self._get_setting("silent") or "1") == "1"

    async def set_silent(self, value: bool) -> None:
        await self._set_setting("silent", "1" if value else "0")

    async def schedule(self) -> Schedule:
        raw = await self._get_setting("schedule")
        return Schedule.from_dict(json.loads(raw), self.config.schedule) if raw else self.config.schedule

    async def update_schedule(self, changes: dict) -> Schedule:
        current = await self.schedule()
        new = Schedule.from_dict(changes, current)
        await self._set_setting("schedule", json.dumps(new.as_dict()))
        return new

    # ---- участники -------------------------------------------------------------------------

    async def touch(self, tg_id: int, name: str, username: str | None, *, employee: bool = False) -> TrainingPerson:
        """Запомнить участника группы (имя и @ обновляются). employee=True — сразу в список
        сотрудников, если владелец его не убирал (/remove)."""
        for _ in range(2):
            try:
                async with self._sessions() as s, s.begin():
                    p = await s.scalar(select(TrainingPerson).where(TrainingPerson.tg_id == tg_id))
                    if p is None:
                        p = TrainingPerson(tg_id=tg_id, name=name[:100] or "без имени", username=username,
                                           created_at=self.now())
                        s.add(p)
                        await s.flush()
                    else:
                        p.name = name[:100] or p.name
                        p.username = username or p.username
                    if employee and not p.excluded:
                        p.is_employee = True
                    return p
            except IntegrityError:
                continue
        raise RuntimeError("не удалось сохранить участника")

    async def person_by_tg(self, tg_id: int) -> TrainingPerson | None:
        async with self._sessions() as s:
            return await s.scalar(select(TrainingPerson).where(TrainingPerson.tg_id == tg_id))

    async def set_employee(self, person_id: int, value: bool, *, by_owner: bool = True) -> None:
        async with self._sessions() as s, s.begin():
            p = await s.get(TrainingPerson, person_id)
            if p is not None:
                p.is_employee = value
                if by_owner:
                    p.excluded = not value  # /remove — больше не добавлять автоматически

    async def set_dm_ok(self, person_id: int) -> None:
        async with self._sessions() as s, s.begin():
            p = await s.get(TrainingPerson, person_id)
            if p is not None:
                p.dm_ok = True

    async def employees(self) -> list[TrainingPerson]:
        async with self._sessions() as s:
            return list(await s.scalars(select(TrainingPerson).where(TrainingPerson.is_employee.is_(True))
                                        .order_by(TrainingPerson.name)))

    async def find(self, query: str, *, employees_only: bool = False) -> list[TrainingPerson]:
        q = query.strip().lstrip("@").lower()
        if not q:
            return []
        async with self._sessions() as s:
            people = list(await s.scalars(select(TrainingPerson)))
        if employees_only:
            people = [p for p in people if p.is_employee]
        exact = [p for p in people if (p.username or "").lower() == q or p.name.lower() == q or str(p.tg_id) == q]
        return exact or [p for p in people if q in p.name.lower() or q in (p.username or "").lower()]

    # ---- записи ----------------------------------------------------------------------------

    def _reschedule_at(self, cap: Caption, created: datetime, sched: Schedule) -> datetime | None:
        if cap.result != "rescheduled":
            return None
        d, t = parse_when(cap.reschedule_text, created.astimezone(self.tz).date())
        if d is None:
            return None
        return datetime.combine(d, t or sched.reschedule_default, tzinfo=self.tz).astimezone(UTC)

    async def add_record(self, person: TrainingPerson, cap: Caption, *, kind: str, file_id: str, chat_id: int,
                         message_id: int, link: str | None) -> TrainingRecord:
        created = self.now()
        sched = await self.schedule()
        async with self._sessions() as s, s.begin():
            existing = await s.scalar(select(TrainingRecord).where(TrainingRecord.chat_id == chat_id,
                                                                   TrainingRecord.message_id == message_id))
            if existing is not None:
                return existing
            rec = TrainingRecord(
                person_id=person.id, caption_name=cap.name, client=cap.client,
                result=cap.result if cap.complete else None, reschedule_text=cap.reschedule_text,
                reschedule_at=self._reschedule_at(cap, created, sched) if cap.complete else None,
                kind=kind, file_id=file_id, chat_id=chat_id, message_id=message_id, link=link, created_at=created,
            )
            s.add(rec)
            await s.flush()
            if not person.is_employee:
                p = await s.get(TrainingPerson, person.id)
                if not p.excluded:
                    p.is_employee = True  # прислал запись — сотрудник
            return rec

    async def update_caption(self, chat_id: int, message_id: int, cap: Caption) -> TrainingRecord | None:
        """Подпись добавили позже (правка сообщения или ответ на свою запись)."""
        sched = await self.schedule()
        async with self._sessions() as s, s.begin():
            rec = await s.scalar(select(TrainingRecord).where(TrainingRecord.chat_id == chat_id,
                                                              TrainingRecord.message_id == message_id))
            if rec is None or not cap.complete:
                return None
            rec.caption_name, rec.client, rec.result = cap.name, cap.client, cap.result
            rec.reschedule_text = cap.reschedule_text
            rec.reschedule_at = self._reschedule_at(cap, _utc(rec.created_at), sched)
            return rec

    async def record_by_message(self, chat_id: int, message_id: int) -> TrainingRecord | None:
        async with self._sessions() as s:
            return await s.scalar(select(TrainingRecord).where(TrainingRecord.chat_id == chat_id,
                                                               TrainingRecord.message_id == message_id))

    async def score(self, record_id: int, comment: str) -> TrainingRecord | None:
        async with self._sessions() as s, s.begin():
            rec = await s.get(TrainingRecord, record_id)
            if rec is None:
                return None
            rec.score = parse_score(comment)
            rec.score_comment = comment.strip()[:4000]
            rec.scored_at = self.now()
            return rec

    async def records_between(self, start: datetime, end: datetime, person_id: int | None = None) -> list[TrainingRecord]:
        q = select(TrainingRecord).where(TrainingRecord.created_at >= start, TrainingRecord.created_at < end)
        if person_id is not None:
            q = q.where(TrainingRecord.person_id == person_id)
        async with self._sessions() as s:
            return list(await s.scalars(q.order_by(TrainingRecord.created_at)))

    async def count_on(self, person_id: int, day: date) -> int:
        start, end = self.day_bounds(day)
        async with self._sessions() as s:
            return await s.scalar(select(func.count(TrainingRecord.id)).where(
                TrainingRecord.person_id == person_id, TrainingRecord.created_at >= start,
                TrainingRecord.created_at < end)) or 0

    async def last_record_day(self, person_id: int) -> date | None:
        async with self._sessions() as s:
            last = await s.scalar(select(func.max(TrainingRecord.created_at)).where(TrainingRecord.person_id == person_id))
        return _utc(last).astimezone(self.tz).date() if last else None

    async def due_reschedules(self, now: datetime) -> list[tuple[TrainingRecord, TrainingPerson]]:
        local_day = now.astimezone(self.tz).date()
        start, _ = self.day_bounds(local_day)
        async with self._sessions() as s:
            rows = await s.execute(
                select(TrainingRecord, TrainingPerson).join(TrainingPerson, TrainingPerson.id == TrainingRecord.person_id)
                .where(TrainingRecord.result == "rescheduled", TrainingRecord.reschedule_at.is_not(None),
                       TrainingRecord.reschedule_at <= now, TrainingRecord.reschedule_at >= start)
            )
            return [(r, p) for r, p in rows.all()]

    async def idle_workdays(self, person: TrainingPerson, day: date, *, include_today: bool) -> int:
        """Сколько рабочих дней подряд (без выходных и отпусков) нет ни одной записи — считая назад от
        вчера (или от сегодня). Дни до появления человека в группе не считаются."""
        sched = await self.schedule()
        since = _utc(person.created_at).astimezone(self.tz).date()
        d = day if include_today else day - timedelta(days=1)
        count = 0
        for _ in range(90):
            if d < since:
                break
            if d.weekday() in sched.work_days and not await self.is_off(person.id, d):
                if await self.count_on(person.id, d):
                    break
                count += 1
            d -= timedelta(days=1)
        return count

    # ---- отсутствия и причины --------------------------------------------------------------

    async def add_absence(self, person_id: int, start: date, end: date) -> None:
        async with self._sessions() as s, s.begin():
            s.add(TrainingAbsence(person_id=person_id, date_from=min(start, end), date_to=max(start, end),
                                  created_at=self.now()))

    async def is_off(self, person_id: int, day: date) -> bool:
        async with self._sessions() as s:
            return bool(await s.scalar(select(func.count(TrainingAbsence.id)).where(
                TrainingAbsence.person_id == person_id, TrainingAbsence.date_from <= day,
                TrainingAbsence.date_to >= day)))

    async def add_reason(self, person_id: int, day: date, text: str, source: str) -> None:
        async with self._sessions() as s, s.begin():
            s.add(TrainingReason(person_id=person_id, day=day, text=text.strip()[:2000], source=source,
                                 created_at=self.now()))

    async def reasons_on(self, day: date) -> dict[int, list[str]]:
        async with self._sessions() as s:
            rows = await s.scalars(select(TrainingReason).where(TrainingReason.day == day).order_by(TrainingReason.id))
            out: dict[int, list[str]] = {}
            for r in rows:
                out.setdefault(r.person_id, []).append(r.text)
            return out

    # ---- журнал проверок -------------------------------------------------------------------

    async def claim(self, kind: str, key: str) -> bool:
        """True — это действие ещё не выполнялось (и теперь отмечено). Защита от дублей при перезапуске."""
        try:
            async with self._sessions() as s, s.begin():
                s.add(TrainingJob(kind=kind, key=key, created_at=self.now()))
            return True
        except IntegrityError:
            return False

    async def job_time(self, kind: str, key: str) -> datetime | None:
        async with self._sessions() as s:
            return _utc(await s.scalar(select(TrainingJob.created_at).where(TrainingJob.kind == kind,
                                                                             TrainingJob.key == key)))

    async def has_job(self, kind: str, key: str) -> bool:
        return await self.job_time(kind, key) is not None

    # ---- статистика -----------------------------------------------------------------------

    async def stats(self, start_day: date, end_day: date, people: list[TrainingPerson] | None = None) -> list[PersonStats]:
        """По каждому сотруднику за дни [start_day, end_day] включительно."""
        people = people if people is not None else await self.employees()
        start, _ = self.day_bounds(start_day)
        _, end = self.day_bounds(end_day)
        records = await self.records_between(start, end)
        out = []
        for p in people:
            st = PersonStats(person=p)
            for r in records:
                if r.person_id != p.id:
                    continue
                st.total += 1
                if r.result is None:
                    st.unsigned += 1
                else:
                    st.by_result[r.result] = st.by_result.get(r.result, 0) + 1
                if r.score is not None:
                    st.scores.append(r.score)
            out.append(st)
        return out
