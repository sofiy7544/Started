"""Время и повторы напоминаний. Всё хранится в UTC, повторы считаются в местном времени
напоминания — «каждый понедельник в 9:00» остаётся 9:00 и после перехода на летнее время."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
WEEKDAYS_NOM = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


class ScheduleError(ValueError):
    """Непонятное или невозможное время — сообщение показывается модели/пользователю."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"неизвестный часовой пояс {name!r}") from exc


def parse_local(value: str, tz: ZoneInfo) -> datetime:
    """«ГГГГ-ММ-ДД ЧЧ:ММ» в местном времени → aware datetime."""
    raw = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ScheduleError("время нужно в формате ГГГГ-ММ-ДД ЧЧ:ММ")


def _at(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=tz)


def _add_month(d: date, anchor_day: int) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))


def next_after(
    current_utc: datetime,
    *,
    repeat: str,
    local_time: str,
    tz: ZoneInfo,
    anchor_day: int | None,
    now_utc: datetime,
) -> datetime | None:
    """Следующее срабатывание после current_utc и строго позже now_utc (пропущенные за время
    простоя повторы не накапливаются). None — повтора нет."""
    if repeat == "none":
        return None
    d = current_utc.astimezone(tz).date()
    for _ in range(4000):
        if repeat == "daily":
            d += timedelta(days=1)
        elif repeat == "weekdays":
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        elif repeat == "weekly":
            d += timedelta(days=7)
        elif repeat == "monthly":
            d = _add_month(d, anchor_day or d.day)
        else:
            raise ScheduleError(f"неизвестный повтор {repeat!r}")
        candidate = _at(d, local_time, tz).astimezone(UTC)
        if candidate > now_utc:
            return candidate
    raise ScheduleError("не удалось вычислить следующее срабатывание")


def human_when(when_utc: datetime, tz: ZoneInfo, now_utc: datetime) -> str:
    """«сегодня в 15:30», «завтра в 10:00», «в пятницу, 8 ноября, в 09:00»."""
    local, today = when_utc.astimezone(tz), now_utc.astimezone(tz).date()
    hhmm = f"{local:%H:%M}"
    delta = (local.date() - today).days
    if delta == 0:
        return f"сегодня в {hhmm}"
    if delta == 1:
        return f"завтра в {hhmm}"
    if delta == 2:
        return f"послезавтра в {hhmm}"
    day = f"{local.day} {MONTHS_GEN[local.month - 1]}"
    if 0 < delta < 7:
        return f"в {WEEKDAYS_ACC[local.weekday()]}, {day}, в {hhmm}"
    year = f" {local.year}" if local.year != today.year else ""
    return f"{day}{year} в {hhmm}"


def human_repeat(repeat: str, local_time: str, weekday: int, anchor_day: int | None) -> str:
    return {
        "none": "",
        "daily": f"каждый день в {local_time}",
        "weekdays": f"по будням в {local_time}",
        "weekly": f"каждую неделю: {WEEKDAYS_NOM[weekday]}, {local_time}",
        "monthly": f"каждый месяц {anchor_day}-го в {local_time}",
    }[repeat]


def calendar_hint(now_utc: datetime, tz: ZoneInfo, days: int = 8) -> str:
    """Подсказка модели: ближайшие даты с днями недели, чтобы «в пятницу» не уехало на неделю."""
    today = now_utc.astimezone(tz).date()
    return ", ".join(
        f"{WEEKDAYS_NOM[(today + timedelta(days=i)).weekday()]} {(today + timedelta(days=i)):%Y-%m-%d}"
        for i in range(days)
    )
