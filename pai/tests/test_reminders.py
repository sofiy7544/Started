"""Напоминания: расписание, сервис, доставка без дублей, восстановление, инструменты модели."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ToolReturnPart
from sqlalchemy import func, select

from pai.db.models import Reminder, ReminderDelivery, Turn
from pai.reminders.delivery import DeliveryError
from pai.reminders.delivery import ReminderDelivery as Delivery
from pai.reminders.schedule import human_when, next_after, parse_local, zone
from pai.reminders.service import ReminderError, ReminderService
from tests.conftest import CLOCK, NOW, OWNER

KYIV = zone("Europe/Kyiv")


def local(s: str) -> datetime:
    return parse_local(s, KYIV).astimezone(UTC)


# ---- расписание --------------------------------------------------------------------------

@pytest.mark.parametrize("repeat,start,expected", [
    ("daily", "2026-11-04 08:00", "2026-11-05 08:00"),
    ("weekdays", "2026-11-06 08:00", "2026-11-09 08:00"),  # пятница → понедельник
    ("weekly", "2026-11-09 09:00", "2026-11-16 09:00"),
    ("monthly", "2026-11-30 10:00", "2026-12-30 10:00"),
])
def test_repeats(repeat, start, expected) -> None:
    nxt = next_after(local(start), repeat=repeat, local_time=start[-5:], tz=KYIV,
                     anchor_day=int(start[8:10]), now_utc=local(start))
    assert nxt == local(expected)


def test_weekly_keeps_local_time_across_dst() -> None:
    before = local("2027-03-22 09:00")  # UTC+2
    after = next_after(before, repeat="weekly", local_time="09:00", tz=KYIV, anchor_day=None, now_utc=before)
    assert after.astimezone(KYIV).strftime("%H:%M") == "09:00" and after - before == timedelta(days=7, hours=-1)


def test_monthly_returns_to_anchor_day() -> None:
    jan31 = local("2027-01-31 10:00")
    feb = next_after(jan31, repeat="monthly", local_time="10:00", tz=KYIV, anchor_day=31, now_utc=jan31)
    mar = next_after(feb, repeat="monthly", local_time="10:00", tz=KYIV, anchor_day=31, now_utc=feb)
    assert feb.astimezone(KYIV).day == 28 and mar.astimezone(KYIV).day == 31


def test_missed_repeats_do_not_pile_up() -> None:
    start = local("2026-11-01 09:00")
    nxt = next_after(start, repeat="daily", local_time="09:00", tz=KYIV, anchor_day=None,
                     now_utc=local("2026-11-04 12:00"))
    assert nxt == local("2026-11-05 09:00")  # пропущенные дни не отправляются пачкой


def test_human_when() -> None:
    now = local("2026-11-04 14:00")
    assert human_when(local("2026-11-04 18:30"), KYIV, now) == "сегодня в 18:30"
    assert human_when(local("2026-11-05 10:00"), KYIV, now) == "завтра в 10:00"
    assert human_when(local("2026-11-06 09:00"), KYIV, now) == "послезавтра в 09:00"
    assert human_when(local("2026-11-08 09:00"), KYIV, now).startswith("в воскресенье, 8 ноября")


# ---- сервис ------------------------------------------------------------------------------

@pytest.fixture
def svc(sessions) -> ReminderService:
    return ReminderService(sessions, now=lambda: CLOCK.now)


async def test_create_absolute_relative_and_validation(svc, assistant) -> None:
    user = await assistant.get_user(OWNER)
    a = await svc.create(user, "позвонить врачу", at_local="2026-11-05 10:00")
    assert a.next_run_at == local("2026-11-05 10:00") and a.when == "завтра в 10:00"
    b = await svc.create(user, "проверить оплату", in_minutes=120)
    assert b.next_run_at == (NOW + timedelta(hours=2)).replace(second=0, microsecond=0)
    for kwargs, msg in [({"at_local": "2026-11-01 10:00"}, "прошло"), ({}, "не указано время"),
                        ({"at_local": "завтра"}, "формате"), ({"in_minutes": 0}, "положительное"),
                        ({"at_local": "2035-01-01 10:00"}, "далеко"),
                        ({"at_local": "2026-11-05 10:00", "repeat": "hourly"}, "повтор")]:
        with pytest.raises(ReminderError, match=msg):
            await svc.create(user, "x", **kwargs)
    with pytest.raises(ReminderError, match="о чём"):
        await svc.create(user, "  ", in_minutes=5)
    assert [v.id for v in await svc.list_active(user.id)] == [b.id, a.id]  # по времени


async def test_update_cancel_snooze_and_isolation(svc, assistant) -> None:
    me, other = await assistant.get_user(OWNER), await assistant.get_user(OWNER + 1)
    r = await svc.create(me, "встреча", at_local="2026-11-06 15:00")
    upd = await svc.update(me, r.id, at_local="2026-11-06 16:30", text="встреча с Олегом", repeat="weekly")
    assert upd.text == "встреча с Олегом" and upd.repeat.startswith("каждую неделю") and "16:30" in upd.when
    for call in (svc.update(other, r.id, text="взлом"), svc.cancel(other.id, r.id), svc.snooze(other.id, r.id, 15)):
        with pytest.raises(ReminderError):
            await call  # чужое напоминание не найти
    snoozed = await svc.snooze(me.id, r.id, 15)
    assert snoozed.id != r.id  # повторяющееся не сдвигается — добавлено разовое на «через 15 минут»
    await svc.cancel(me.id, r.id)
    assert [v.id for v in await svc.list_active(me.id)] == [snoozed.id]


# ---- доставка ----------------------------------------------------------------------------

class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.errors: list[DeliveryError] = []
        self.delay = 0.0

    async def send_reminder(self, tg_id, due, text):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((tg_id, text))
        return 1000 + len(self.sent)


@pytest.fixture
def delivery(svc, sessions, assistant):
    sender = FakeSender()
    return Delivery(service=svc, sessions=sessions, sender=sender, on_sent=assistant.record_outgoing,
                    late_note="(с опозданием)"), sender


async def deliveries(sessions) -> list[tuple[str, int]]:
    async with sessions() as s:
        return [(d.status, d.attempts) for d in await s.scalars(select(ReminderDelivery).order_by(ReminderDelivery.id))]


async def test_one_off_reminder_is_sent_once_at_time(delivery, svc, assistant, sessions) -> None:
    d, sender = delivery
    user = await assistant.get_user(OWNER)
    r = await svc.create(user, "позвонить врачу", in_minutes=30)
    assert await d.tick() == 0  # ещё рано
    CLOCK.advance(minutes=30)
    assert await d.tick() == 1 and sender.sent == [(OWNER, "⏰ Напоминание: позвонить врачу")]
    CLOCK.advance(minutes=1)
    assert await d.tick() == 0  # не дублируется
    async with sessions() as s:
        stored = await s.get(Reminder, r.id)
    assert stored.status == "done" and stored.sent_count == 1
    assert await deliveries(sessions) == [("sent", 1)]


async def test_recurring_reminder_moves_to_next_occurrence(delivery, svc, assistant, sessions) -> None:
    d, sender = delivery
    user = await assistant.get_user(OWNER)
    r = await svc.create(user, "проверить отчёты", at_local="2026-11-09 09:00", repeat="weekly")
    CLOCK.now = local("2026-11-09 09:00")
    await d.tick()
    async with sessions() as s:
        stored = await s.get(Reminder, r.id)
    assert stored.status == "active" and stored.next_run_at.replace(tzinfo=UTC) == local("2026-11-16 09:00")


async def test_parallel_ticks_do_not_duplicate(delivery, svc, assistant, sessions) -> None:
    d, sender = delivery
    sender.delay = 0.05
    user = await assistant.get_user(OWNER)
    await svc.create(user, "одно напоминание", in_minutes=1)
    CLOCK.advance(minutes=1)
    results = await asyncio.gather(d.tick(), d.tick(), d.tick())
    assert sum(results) == 1 and len(sender.sent) == 1


async def test_temporary_failure_is_retried_permanent_is_not(delivery, svc, assistant, sessions) -> None:
    d, sender = delivery
    user = await assistant.get_user(OWNER)
    await svc.create(user, "сеть шалит", in_minutes=1)
    CLOCK.advance(minutes=1)
    sender.errors = [DeliveryError("network", permanent=False)]
    assert await d.tick() == 0
    assert await d.tick() == 1  # повтор на следующем шаге
    assert await deliveries(sessions) == [("sent", 2)]

    await svc.create(user, "бот заблокирован", in_minutes=1)
    CLOCK.advance(minutes=1)
    sender.errors = [DeliveryError("forbidden", permanent=True)]
    await d.tick()
    await d.tick()
    assert len(sender.sent) == 1 and (await deliveries(sessions))[-1] == ("failed", 5)


async def test_restart_recovers_overdue_and_stuck(delivery, svc, assistant, sessions) -> None:
    d, sender = delivery
    user = await assistant.get_user(OWNER)
    await svc.create(user, "пока бот был выключен", in_minutes=10)
    stuck = await svc.create(user, "упал посреди отправки", in_minutes=10)
    CLOCK.advance(hours=3)  # бот «лежал» 3 часа
    async with sessions() as s, s.begin():  # имитация: процесс упал после записи в журнал, до отправки
        s.add(ReminderDelivery(reminder_id=stuck.id, user_id=user.id, scheduled_for=stuck.next_run_at,
                               status="sending", created_at=CLOCK.now - timedelta(minutes=5)))
    fresh = Delivery(service=ReminderService(sessions, now=lambda: CLOCK.now), sessions=sessions, sender=sender,
                     late_note="(с опозданием)")  # «новый процесс» на той же базе
    assert await fresh.tick() == 2
    assert all("(с опозданием)" in text for _, text in sender.sent)


async def test_sent_reminder_is_part_of_conversation(delivery, svc, assistant, script, sessions) -> None:
    d, _ = delivery
    user = await assistant.get_user(OWNER)
    await svc.create(user, "выпить таблетку", in_minutes=1)
    CLOCK.advance(minutes=1)
    await d.tick()
    await assistant.ask(user, "сделал")
    assert "⏰ Напоминание: выпить таблетку" in script.seen[-1].assistant_texts  # модель видит контекст
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Turn).where(Turn.user_text == "")) == 1
    assert await assistant.last_user_activity(user.id) is not None


# ---- инструменты модели -------------------------------------------------------------------

def tool_results(script) -> list[str]:
    return [p.content for m in script.seen[-1].messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart)]


async def test_model_creates_reminder_via_tool(assistant, script, sessions) -> None:
    user = await assistant.get_user(OWNER)
    script.replies = [{"tool": "create_reminder", "args": {"text": "позвонить врачу", "at_local": "2026-11-05 10:00"}},
                      "Хорошо, напомню завтра в 10."]
    answer = await assistant.ask(user, "Напомни мне завтра в 10:00 позвонить врачу")
    assert [v.text for v in answer.reminders.created] == ["позвонить врачу"]
    assert tool_results(script)[0].startswith("Создано #")
    instr = script.seen[0].instructions
    assert "create_reminder" in instr and "Europe/Kyiv" in instr and "четверг 2026-11-05" in instr
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Reminder)) == 1


async def test_tool_errors_go_back_to_model_without_effects(assistant, script) -> None:
    user = await assistant.get_user(OWNER)
    script.replies = [{"tool": "create_reminder", "args": {"text": "встреча"}}, "Во сколько напомнить?"]
    answer = await assistant.ask(user, "В пятницу напомни про встречу")
    assert answer.reminders is None and answer.text == "Во сколько напомнить?"
    assert "Не создано: не указано время" in tool_results(script)[0]


async def test_model_updates_and_deletes_only_own(assistant, script, svc) -> None:
    me, other = await assistant.get_user(OWNER), await assistant.get_user(OWNER + 1)
    mine = await svc.create(me, "моё", in_minutes=60)
    theirs = await svc.create(other, "чужое", in_minutes=60)
    script.replies = [{"tool": "update_reminder", "args": {"reminder_id": mine.id, "in_minutes": 90}},
                      {"tool": "delete_reminder", "args": {"reminder_id": theirs.id}}, "Готово."]
    answer = await assistant.ask(me, f"перенеси на полтора часа и удали #{theirs.id}")
    assert [v.id for v in answer.reminders.updated] == [mine.id] and answer.reminders.deleted == []
    assert "Не удалено" in tool_results(script)[1]
    assert [v.text for v in await svc.list_active(other.id)] == ["чужое"]


async def test_reminders_survive_full_wipe_correctly(assistant, svc, sessions) -> None:
    user = await assistant.get_user(OWNER)
    await svc.create(user, "x", in_minutes=5)
    await assistant.wipe_user(user)
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Reminder)) == 0  # удаляются вместе с данными

