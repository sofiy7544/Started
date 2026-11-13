"""Плановые действия: проверка в первой половине дня, личка без ответа, напоминания о переносах,
дневной и недельный отчёты. Раз в 30 с проверяет, что пора сделать; каждое действие отмечается в
training_jobs (UNIQUE) — после перезапуска ничего не дублируется и не теряется в течение дня."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Protocol

from pai.training import reports
from pai.training.reports import workdays_text
from pai.training.service import TrainingService

log = logging.getLogger(__name__)


class TrainingSender(Protocol):
    async def to_group(self, chat_id: int, html: str) -> None: ...

    async def to_owner(self, html: str) -> None: ...

    async def to_person(self, tg_id: int, html: str) -> bool: ...


def mention(person) -> str:  # type: ignore[no-untyped-def]
    return f'<a href="tg://user?id={person.tg_id}">{escape(person.name)}</a>'


class TrainingScheduler:
    def __init__(self, svc: TrainingService, sender: TrainingSender) -> None:
        self.svc = svc
        self.sender = sender

    async def tick(self) -> None:
        svc = self.svc
        now = svc.now()
        local = now.astimezone(svc.tz)
        day = local.date()
        sched = await svc.schedule()
        group = await svc.group_id()
        workday = local.weekday() in sched.work_days
        t = local.time()
        cfg = svc.config

        quiet = await svc.silent()  # тихий режим: в группу и сотрудникам — ничего, отчёты владельцу — да

        # 1. Проверка: нет ни одной записи idle_days рабочих дней подряд (и сегодня пока тоже) —
        #    одно упоминание в группе на весь такой период. Одна запись за эти дни — норма.
        if group and workday and not quiet and sched.first_check <= t < sched.work_end:
            for p in await svc.employees():
                if await svc.is_off(p.id, day) or await svc.count_on(p.id, day):
                    continue
                idle = await svc.idle_workdays(p, day, include_today=False)
                if idle < sched.idle_days:
                    continue
                last = await svc.last_record_day(p.id) or "never"
                if not await svc.claim("streak", f"{p.id}:{last}"):
                    continue  # по этому периоду уже упоминали — без лишнего давления
                await svc.claim("ping", f"{day}:{p.id}")
                text = escape(cfg.text("no_records_group", name="{name}", days=workdays_text(idle)))
                await self.sender.to_group(group, text.replace("{name}", mention(p)))

        # 2. Нет ответа и записей за followup_minutes после упоминания — в личку (если запускал бота).
        if workday and not quiet:
            for p in await svc.employees():
                pinged = await svc.job_time("ping", f"{day}:{p.id}")
                if pinged is None or now < pinged + timedelta(minutes=sched.followup_minutes):
                    continue
                if not p.dm_ok or await svc.count_on(p.id, day) or (await svc.reasons_on(day)).get(p.id):
                    continue
                if await svc.claim("dm", f"{day}:{p.id}"):
                    await self.sender.to_person(p.tg_id, escape(cfg.text("no_records_dm")))

        # 3. Напоминания о переносах — в день переноса.
        for rec, p in [] if quiet else await svc.due_reschedules(now):
            if not await svc.claim("resched", str(rec.id)):
                continue
            when = rec.reschedule_at.astimezone(svc.tz) if rec.reschedule_at.tzinfo else rec.reschedule_at
            text = escape(cfg.text("reschedule_reminder", client=rec.client or "—", when=f"{when:%H:%M}"))
            if not (p.dm_ok and await self.sender.to_person(p.tg_id, text)) and group:
                await self.sender.to_group(group, f"{mention(p)}, {text[0].lower()}{text[1:]}")

        # 4. Дневной отчёт.
        if workday and t >= sched.daily_report and await svc.claim("daily", str(day)):
            await self.sender.to_owner(await reports.daily_report(svc, day))

        # 5. Недельный отчёт.
        if local.weekday() == sched.weekly_report_day and t >= sched.weekly_report:
            week = local.isocalendar()
            if await svc.claim("weekly", f"{week.year}-W{week.week}"):
                await self.sender.to_owner(await reports.weekly_report(svc, day))

    async def run_forever(self, interval: float = 30.0) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — сбой проверки не должен останавливать бота
                log.exception("training tick failed")
            await asyncio.sleep(interval)


def now_utc() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
