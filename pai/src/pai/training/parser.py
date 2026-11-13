"""Разбор подписи к записи: «Имя | Клиент | Результат» в вольном формате и дата переноса."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

RESULTS = ("closed", "rescheduled", "refused", "no_answer")
RESULT_TITLES = {"closed": "✅ закрыл", "rescheduled": "🔄 перенос", "refused": "❌ отказ", "no_answer": "📵 не дозвонился"}

# Порядок важен: «не дозвонился» проверяем раньше, чем «отказ» и т. п.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("no_answer", re.compile(r"📵|недозвон|не\s*дозвон|не\s*бер[её]т|не\s*взял|нет\s*ответа|не\s*отвеча|сброс|абонент\s*недоступ|не\s*додзвон", re.I)),
    ("rescheduled", re.compile(r"🔄|перенос|перен[её]с|перезвон|передзвон|позже|пізніше|перенес", re.I)),
    ("refused", re.compile(r"❌|отказ|відмов|не\s*интерес|не\s*цікав|не\s*нужно|не\s*потрібно", re.I)),
    ("closed", re.compile(r"✅|закрыл|закрила|закрыла|закрито|закрыто|продал|продала|продаж|оформил|оформила|сделка|угода|купил|купила", re.I)),
]
_SEPARATORS = re.compile(r"\s*(?:\||;|/|\n|—|–|\s-\s)\s*")

WEEKDAYS = {
    0: ("понедельник", "пн", "понеділок"), 1: ("вторник", "вт", "вівторок"), 2: ("среда", "среду", "ср", "середа", "середу"),
    3: ("четверг", "чт", "четвер"), 4: ("пятница", "пятницу", "пт", "п'ятниця", "п'ятницю", "пятниця"),
    5: ("суббота", "субботу", "сб", "субота", "суботу"), 6: ("воскресенье", "вс", "неділя", "неділю", "нд"),
}


@dataclass(frozen=True)
class Caption:
    name: str | None
    client: str | None
    result: str | None
    reschedule_text: str | None  # «на пятницу 15:00» как написано

    @property
    def complete(self) -> bool:
        return self.result is not None and bool(self.client)


def detect_result(text: str) -> tuple[str | None, re.Match[str] | None]:
    for key, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            return key, m
    return None, None


def parse_caption(text: str | None) -> Caption:
    raw = (text or "").strip()
    if not raw:
        return Caption(None, None, None, None)
    parts = [p.strip(" .,:") for p in _SEPARATORS.split(raw) if p and p.strip(" .,:")]
    result = None
    result_idx = None
    for i, part in enumerate(parts):
        key, _m = detect_result(part)
        if key:
            result, result_idx = key, i
            break
    reschedule = None
    if result == "rescheduled" and result_idx is not None:
        tail = " ".join(parts[result_idx:])
        _k, m = detect_result(tail)
        after = re.sub(r"^\w*", "", tail[m.end():]).strip(" :,-") if m else ""  # «перенесли», «перезвонить»
        reschedule = after or None
    others = [p for i, p in enumerate(parts) if i != result_idx]
    if result is not None and result_idx is not None and len(parts) == 1:
        # «Петров отказ» — без разделителей: всё, кроме слова результата, — клиент.
        _k, m = detect_result(parts[0])
        rest = (parts[0][:m.start()] + " " + (parts[0][m.end():] if result != "rescheduled" else "")).strip(" .,:-") if m else ""
        others = [rest] if rest else []
    if result is not None and result_idx is not None and result_idx < len(parts) - 1 and result == "rescheduled":
        # «перенос | на пятницу» — хвост после результата относится к переносу, а не к клиенту.
        others = [p for i, p in enumerate(parts) if i < result_idx]
        if reschedule is None or reschedule == "":
            reschedule = " ".join(parts[result_idx + 1:]) or None
    name = client = None
    if len(others) >= 2:
        name, client = others[0], " ".join(others[1:])
    elif len(others) == 1:
        client = others[0]
    return Caption(name[:100] if name else None, client[:200] if client else None, result,
                   reschedule[:100] if reschedule else None)


_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):(\d{2})(?!\d)|(?:в|о|на)\s+([01]?\d|2[0-3])\s*(?:час|ч\b|:00)", re.I)
_DATE = re.compile(r"(?<![\d:.])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])")


def parse_when(text: str | None, today: date) -> tuple[date | None, time | None]:
    """«на завтра», «пятница 15:00», «15.11», «через 2 дня», «в 14:00» → дата (и время, если есть)."""
    if not text:
        return None, None
    s = text.lower()
    t = None
    for m in _TIME.finditer(s):
        if m.group(1):
            t = time(int(m.group(1)), int(m.group(2)))
        elif m.group(3):
            t = time(int(m.group(3)), 0)
    s_no_time = _TIME.sub(" ", s)
    d: date | None = None
    m = _DATE.search(s_no_time)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
            if not m.group(3) and d < today:
                d = date(year + 1, month, day)
        except ValueError:
            d = None
    if d is None:
        if "послезавтра" in s or "післязавтра" in s:
            d = today + timedelta(days=2)
        elif "завтра" in s:
            d = today + timedelta(days=1)
        elif "сегодня" in s or "сьогодні" in s:
            d = today
        else:
            m2 = re.search(r"через\s+(\d+)\s*(?:дн|день|дня|днів|дней)", s)
            if m2:
                d = today + timedelta(days=int(m2.group(1)))
            elif re.search(r"через\s+недел|через\s+тиждень", s):
                d = today + timedelta(days=7)
            else:
                words = re.findall(r"[\w']+", s)
                for wd, names in WEEKDAYS.items():
                    if any(w in names for w in words):
                        ahead = (wd - today.weekday()) % 7 or 7
                        d = today + timedelta(days=ahead)
                        break
    if d is None and t is not None:
        d = today
    return d, t


def kyiv_datetime(d: date, t: time | None, tz, default: time) -> datetime:  # type: ignore[no-untyped-def]
    return datetime.combine(d, t or default, tzinfo=tz)
