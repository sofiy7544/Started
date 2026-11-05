"""Инициативные сообщения: правила частоты и тишины, повод из памяти, без повторов, отдельный цикл."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic_ai import exceptions as E
from sqlalchemy import select

from pai.db.models import ProactiveMessage, UsageRecord
from pai.proactive.service import ProactiveService, in_quiet_hours
from pai.reminders.delivery import ReminderDelivery
from pai.reminders.service import ReminderService
from tests.conftest import CLOCK, NOW, OWNER

# NOW = 2026-11-04 12:00 UTC = 14:00 по Киеву — не тихие часы.


class FakeSender:
    def __init__(self) -> None:
        self.proactive: list[str] = []
        self.reminders: list[str] = []
        self.fail = False

    async def send_proactive(self, tg_id, text):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("telegram down")
        self.proactive.append(text)
        return 1

    async def send_reminder(self, tg_id, due, text):  # type: ignore[no-untyped-def]
        self.reminders.append(text)
        return 2


@pytest.fixture
def pro(assistant, sessions, settings):
    def make(**kw):  # type: ignore[no-untyped-def]
        return ProactiveService(
            sessions=sessions, settings=settings, catalog=assistant.catalog, engine=assistant._engine,
            budget=kw.get("budget", assistant.budget), memory=assistant.memory.store, reminders=assistant.reminders,
            last_user_activity=assistant.last_user_activity, now=lambda: CLOCK.now)
    return make


async def enabled_user(assistant, **prefs):  # type: ignore[no-untyped-def]
    user = await assistant.get_user(OWNER)
    await assistant.set_schedule_prefs(user, proactive_enabled=True, **prefs)
    await assistant.memory.store.add(user.id, "project", "Запускает лендинг для кофейни, дедлайн 15 ноября")
    await assistant.memory.store.add(user.id, "agreement", "Обещал отправить отчёт клиенту до пятницы")
    return user


def draft(text="Как продвигается лендинг для кофейни? До дедлайна 15 ноября осталось 11 дней.",
          topic="лендинг кофейня статус", send=True, kind="followup") -> dict:
    return {"send": send, "kind": kind, "topic": topic, "text": text}


def test_quiet_hours_across_midnight_and_daytime(assistant) -> None:
    from pai.db.models import User

    u = User(tg_id=1, timezone="Europe/Kyiv", quiet_start=22 * 60, quiet_end=9 * 60)
    at = lambda hhmm: NOW.replace(hour=int(hhmm[:2]) - 2, minute=int(hhmm[3:]))  # noqa: E731 — Киев = UTC+2
    assert in_quiet_hours(u, at("23:30")) and in_quiet_hours(u, at("08:59")) and not in_quiet_hours(u, at("09:00"))
    u.quiet_start, u.quiet_end = 13 * 60, 15 * 60  # тихие часы днём
    assert in_quiet_hours(u, at("14:00")) and not in_quiet_hours(u, at("16:00")) and not in_quiet_hours(u, at("12:00"))
    u.quiet_start = u.quiet_end = 0
    assert not in_quiet_hours(u, at("03:00"))


async def test_sends_relevant_message_and_records_it(assistant, script, pro, sessions) -> None:
    user = await enabled_user(assistant)
    script.memory_replies = [draft()]
    sender = FakeSender()
    row = await pro().run_for(user, sender, assistant.record_outgoing)
    assert row is not None and sender.proactive == [draft()["text"]]
    context = script.memory_seen[-1].user_prompts[0]
    assert "лендинг для кофейни" in context and "отчёт клиенту" in context  # повод — из памяти
    async with sessions() as s:
        assert await s.scalar(select(UsageRecord.operation).where(UsageRecord.operation == "proactive"))
    await assistant.ask(user, "ещё не закончил")
    assert draft()["text"] in script.seen[-1].assistant_texts  # ответ владельца понятен модели


async def test_rules_block_without_calling_model(assistant, script, pro) -> None:
    user = await assistant.get_user(OWNER)
    s = pro()
    assert (await s.check(user)).reason == "выключено"
    user = await enabled_user(assistant)
    CLOCK.now = NOW.replace(hour=21)  # 23:00 Киев
    assert (await s.check(user)).reason == "тихие часы"
    CLOCK.now = NOW
    await assistant.ask(user, "привет")  # владелец только что писал
    assert (await s.check(user)).reason == "владелец недавно писал"
    CLOCK.advance(hours=3)
    assert (await s.check(user)).send
    assert len(script.memory_seen) == 0


async def test_frequency_and_unanswered_pause(assistant, script, pro) -> None:
    # раз в 8 часов; тихие часы выключены, чтобы «перемотка» времени не попадала на ночь
    user = await enabled_user(assistant, proactive_frequency="often", quiet_start=0, quiet_end=0)
    s, sender = pro(), FakeSender()
    script.memory_replies = [draft(topic="тема 1", text="Первый вопрос про лендинг?"),
                             draft(topic="тема 2", text="Как дела с отчётом клиенту?"),
                             draft(topic="тема 3", text="Третье сообщение")]
    assert await s.run_for(user, sender) is not None
    CLOCK.advance(hours=4)
    assert (await s.check(user)).reason == "рано"
    CLOCK.advance(hours=6)
    assert await s.run_for(user, sender) is not None
    CLOCK.advance(hours=12)
    assert (await s.check(user)).reason == "нет ответа на прошлые сообщения"  # два без ответа — пауза
    await assistant.ask(user, "привет, был занят")
    CLOCK.advance(hours=3)
    assert (await s.check(user)).send


async def test_no_reason_no_message(assistant, script, pro, sessions) -> None:
    user = await assistant.get_user(OWNER)
    await assistant.set_schedule_prefs(user, proactive_enabled=True)  # памяти о делах нет
    assert await pro().run_for(user, FakeSender()) is None
    assert script.memory_seen == []  # модель даже не вызывалась — случайных сообщений нет
    async with sessions() as s:
        assert (await s.scalar(select(ProactiveMessage.status))) == "skipped"
    CLOCK.advance(hours=6)
    assert (await pro().check(user)).reason == "рано"  # «нечего сказать» не проверяется каждые 10 минут


async def test_repeats_and_model_refusals_are_skipped(assistant, script, pro) -> None:
    user = await enabled_user(assistant, proactive_frequency="often", quiet_start=0, quiet_end=0)
    s, sender = pro(), FakeSender()
    script.memory_replies = [draft(), draft(text="Как продвигается лендинг для кофейни? До дедлайна 15 ноября осталось 11 дней!"),
                             draft(send=False), E.ModelOverloadedError("m", "x")]
    await s.run_for(user, sender)
    for _ in range(3):
        await assistant.ask(user, "ответ")  # чтобы не включалась пауза «нет ответа»
        CLOCK.advance(hours=12)
        await s.run_for(user, sender)
    assert len(sender.proactive) == 1  # повтор темы, «нечего сказать» и ошибка модели не отправлены


async def test_delivery_failure_and_budget_guard(assistant, script, pro, sessions) -> None:
    user = await enabled_user(assistant)
    sender = FakeSender()
    sender.fail = True
    script.memory_replies = [draft()]
    assert await pro().run_for(user, sender) is None
    async with sessions() as s:
        assert (await s.scalar(select(ProactiveMessage.status).order_by(ProactiveMessage.id.desc()))) == "failed"
    from pai.services.budget import BudgetGuard

    poor = BudgetGuard(sessions, monthly_limit_usd=0.0001)
    async with sessions() as s, s.begin():
        s.add(UsageRecord(provider="x", model_id="x", operation="chat", cost_usd=Decimal("0.0001")))
    CLOCK.advance(days=2)
    assert (await pro(budget=poor).check(user)).reason == "бюджет почти исчерпан"


async def test_reminders_are_not_delayed_by_slow_proactive(assistant, script, pro, sessions) -> None:
    """Отдельные циклы: долгая генерация инициативы не задерживает напоминание."""
    user = await enabled_user(assistant)
    reminders = ReminderService(sessions, now=lambda: CLOCK.now)
    await reminders.create(user, "важное", in_minutes=1)
    sender = FakeSender()
    original = assistant._engine.structured

    async def slow_structured(*a, **kw):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1.0)
        return await original(*a, **kw)

    assistant._engine.structured = slow_structured
    script.memory_replies = [draft()]
    CLOCK.advance(minutes=1)
    delivery = ReminderDelivery(service=reminders, sessions=sessions, sender=sender)
    slow = asyncio.create_task(pro().run_for(user, sender))
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    await delivery.tick()
    assert sender.reminders == ["⏰ Напоминание: важное"]
    assert asyncio.get_running_loop().time() - started < 0.5
    await slow
    assert len(sender.proactive) == 1

