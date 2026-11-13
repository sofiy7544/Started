"""Тексты отчётов владельцу и статистики сотруднику. Только цифры и факты из таблиц training_*."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

from pai.training.parser import RESULT_TITLES, RESULTS
from pai.training.service import PersonStats, TrainingService


def workdays_text(n: int) -> str:
    word = "рабочий день" if n % 10 == 1 and n % 100 != 11 else (
        "рабочих дня" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "рабочих дней")
    return f"{n} {word}"


def _results_line(st: PersonStats) -> str:
    parts = [f"{RESULT_TITLES[r]} {st.by_result[r]}" for r in RESULTS if st.by_result.get(r)]
    if st.unsigned:
        parts.append(f"без подписи {st.unsigned}")
    return ", ".join(parts)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _score(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}"


def _delta(now: float | None, before: float | None, pct: bool = False) -> str:
    if now is None or before is None:
        return ""
    d = now - before
    if abs(d) < 1e-9:
        return " (без изменений)"
    arrow = "▲" if d > 0 else "▼"
    return f" ({arrow} {abs(d) * 100:.0f} п.п.)" if pct else f" ({arrow} {abs(d):.1f})"


async def previous_workday(svc: TrainingService, day: date) -> date:
    sched = await svc.schedule()
    d = day - timedelta(days=1)
    for _ in range(7):
        if d.weekday() in sched.work_days:
            return d
        d -= timedelta(days=1)
    return day - timedelta(days=1)


async def daily_report(svc: TrainingService, day: date, *, late: bool = False) -> str:
    stats = await svc.stats(day, day)
    reasons = await svc.reasons_on(day)
    lines = [f"📊 Отчёт за {day:%d.%m.%Y}" + (" (с опозданием — бот был выключен)" if late else "")]
    if await svc.no_data(day):
        lines[0] += "\n⚠️ Бот был выключен больше суток — часть записей за этот день могла не дойти"
    if await svc.silent():
        lines[0] += "\n🔇 Тихий режим: в группе бот молчит (включить ответы — /silent off)"
    if not stats:
        return lines[0] + "\nСотрудников в списке пока нет — они появятся после первой записи в группе."
    total = sum(s.total for s in stats)
    lines.append(f"Всего записей: {total}")
    warnings = []
    for st in stats:
        name = escape(st.person.name)
        off = await svc.is_off(st.person.id, day)
        if off:
            lines.append(f"\n• {name}: выходной/отпуск")
            continue
        if st.total:
            lines.append(f"\n• {name}: {st.total} — {_results_line(st)}")
        else:
            why = "; ".join(escape(r) for r in reasons.get(st.person.id, []))
            lines.append(f"\n• {name}: 0 записей" + (f" — {why}" if why else ""))
            idle = await svc.idle_workdays(st.person, day, include_today=True)
            if idle >= (await svc.schedule()).idle_days:
                warnings.append(f"⚠️ {name}: нет записей {workdays_text(idle)} подряд"
                                + ("" if why else ", причину не сообщил"))
        if st.unsigned:
            lines.append(f"   записей без подписи: {st.unsigned}")
    if warnings:
        lines.append("\nВнимание:\n" + "\n".join(warnings))
    return "\n".join(lines)


async def weekly_report(svc: TrainingService, last_day: date, *, late: bool = False) -> str:
    start = last_day - timedelta(days=6)
    stats = await svc.stats(start, last_day)
    before = {s.person.id: s for s in await svc.stats(start - timedelta(days=7), start - timedelta(days=1))}
    lines = [f"📈 Неделя {start:%d.%m}–{last_day:%d.%m}" + (" (с опозданием — бот был выключен)" if late else "")]
    if not stats:
        return lines[0] + "\nСотрудников в списке пока нет."
    tot = sum(s.total for s in stats)
    tot_prev = sum(s.total for s in before.values())
    lines.append(f"Всего записей: {tot} (прошлая неделя: {tot_prev})")
    for st in stats:
        b = before.get(st.person.id)
        trend = ""
        if b is not None:
            trend = " ▲" if st.total > b.total else " ▼" if st.total < b.total else " ="
        lines.append(
            f"\n• {escape(st.person.name)}: записей {st.total}{trend} (было {b.total if b else 0})\n"
            f"   закрытия: {_pct(st.closed_share)}{_delta(st.closed_share, b.closed_share if b else None, True)}\n"
            f"   средняя оценка: {_score(st.avg_score)}{_delta(st.avg_score, b.avg_score if b else None)}"
        )
    return "\n".join(lines)


async def employee_history(svc: TrainingService, person) -> str:  # type: ignore[no-untyped-def]
    person_id, name = person.id, person.name
    today = svc.local_now().date()
    start, end = svc.day_bounds(today - timedelta(days=27))[0], svc.day_bounds(today)[1]
    records = await svc.records_between(start, end, person_id)
    lines = [f"👤 {escape(name)} — последние 4 недели"]
    for w in range(3, -1, -1):
        w_end = today - timedelta(days=7 * w)
        w_start = w_end - timedelta(days=6)
        s = (await svc.stats(w_start, w_end, [person]))[0]
        lines.append(f"{w_start:%d.%m}–{w_end:%d.%m}: записей {s.total}, закрытия {_pct(s.closed_share)}, "
                         f"оценка {_score(s.avg_score)}")
    lines.append("\nПоследние записи:")
    if not records:
        lines.append("нет")
    for r in records[-15:]:
        when = r.created_at.astimezone(svc.tz) if r.created_at.tzinfo else r.created_at
        res = RESULT_TITLES.get(r.result or "", "без подписи")
        line = f"{when:%d.%m %H:%M} · {escape(r.client or '—')} · {res}"
        if r.score is not None:
            line += f" · оценка {r.score:g}"
        if r.link:
            line += f' · <a href="{r.link}">запись</a>'
        lines.append(line)
        if r.score_comment:
            lines.append(f"   💬 {escape(r.score_comment[:200])}")
    return "\n".join(lines)


async def my_stats(svc: TrainingService, person) -> str:  # type: ignore[no-untyped-def]
    today = svc.local_now().date()
    people = [person]
    s = (await svc.stats(today - timedelta(days=6), today, people))[0]
    b = (await svc.stats(today - timedelta(days=13), today - timedelta(days=7), people))[0]
    return (f"📊 Твоя неделя ({today - timedelta(days=6):%d.%m}–{today:%d.%m}):\n"
            f"Записей: {s.total} (прошлая неделя: {b.total})\n{_results_line(s) or '—'}\n"
            f"Доля закрытий: {_pct(s.closed_share)}\nСредняя оценка владельца: {_score(s.avg_score)}")
