"""Настройки контроля обучения: файл config/training.toml + изменения владельца из базы (/schedule)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from pai.config import PROJECT_ROOT, ConfigError

DEFAULT_FILE = PROJECT_ROOT / "config" / "training.toml"
WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def hm(value: str) -> time:
    try:
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"время должно быть в формате ЧЧ:ММ: {value!r}") from exc


@dataclass(frozen=True)
class Schedule:
    work_start: time = time(10, 0)
    work_end: time = time(19, 0)
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)
    first_check: time = time(13, 0)
    followup_minutes: int = 60
    daily_report: time = time(19, 30)
    weekly_report_day: int = 6
    weekly_report: time = time(20, 0)
    reschedule_default: time = time(10, 0)

    def as_dict(self) -> dict:
        return {"work_start": f"{self.work_start:%H:%M}", "work_end": f"{self.work_end:%H:%M}",
                "work_days": list(self.work_days), "first_check": f"{self.first_check:%H:%M}",
                "followup_minutes": self.followup_minutes, "daily_report": f"{self.daily_report:%H:%M}",
                "weekly_report_day": self.weekly_report_day, "weekly_report": f"{self.weekly_report:%H:%M}",
                "reschedule_default": f"{self.reschedule_default:%H:%M}"}

    @classmethod
    def from_dict(cls, data: dict, base: Schedule | None = None) -> Schedule:
        s = base or cls()
        upd: dict = {}
        for key in ("work_start", "work_end", "first_check", "daily_report", "weekly_report", "reschedule_default"):
            if key in data:
                upd[key] = hm(data[key])
        if "work_days" in data:
            days = tuple(sorted({int(d) for d in data["work_days"] if 0 <= int(d) <= 6}))
            if not days:
                raise ConfigError("нужен хотя бы один рабочий день")
            upd["work_days"] = days
        if "followup_minutes" in data:
            upd["followup_minutes"] = max(5, min(int(data["followup_minutes"]), 600))
        if "weekly_report_day" in data:
            upd["weekly_report_day"] = int(data["weekly_report_day"]) % 7
        return replace(s, **upd)

    def describe(self) -> str:
        days = ", ".join(WEEKDAY_SHORT[d] for d in self.work_days)
        return (f"Рабочие часы: {self.work_start:%H:%M}–{self.work_end:%H:%M}\nРабочие дни: {days}\n"
                f"Первая проверка: {self.first_check:%H:%M}\nЛичка, если нет ответа: через {self.followup_minutes} мин\n"
                f"Дневной отчёт: {self.daily_report:%H:%M}\n"
                f"Недельный отчёт: {WEEKDAY_SHORT[self.weekly_report_day]} {self.weekly_report:%H:%M}\n"
                f"Перенос без времени — напоминание в {self.reschedule_default:%H:%M}")


@dataclass(frozen=True)
class TrainingConfig:
    enabled: bool
    tz: ZoneInfo
    schedule: Schedule
    texts: dict[str, str] = field(default_factory=dict)

    def text(self, key: str, **kw: object) -> str:
        return self.texts[key].format(**kw) if kw else self.texts[key]


REQUIRED_TEXTS = ("ask_caption", "ask_reschedule_date", "accepted", "accepted_reaction", "no_records_group",
                  "no_records_dm", "reason_saved", "reschedule_reminder", "question_redirect", "employee_start",
                  "not_employee", "tips")


def load_training_config(path: Path = DEFAULT_FILE) -> TrainingConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TrainingConfig(False, ZoneInfo("Europe/Kyiv"), Schedule())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"ошибка в {path}: {exc}") from exc
    texts = {k: str(v) for k, v in data.get("texts", {}).items()}
    missing = [k for k in REQUIRED_TEXTS if k not in texts]
    if missing:
        raise ConfigError(f"в {path} нет текстов: {', '.join(missing)}")
    return TrainingConfig(
        enabled=bool(data.get("enabled", True)),
        tz=ZoneInfo(data.get("timezone", "Europe/Kyiv")),
        schedule=Schedule.from_dict(data.get("schedule", {})),
        texts=texts,
    )
