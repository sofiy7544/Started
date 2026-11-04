"""Самодиагностика: возможности компьютера и настройки бота. Секреты не выводятся."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

from pai.config import PROJECT_ROOT


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return status.ullTotalPhys / 1024**3
        with open("/proc/meminfo", encoding="ascii") as f:
            return int(f.readline().split()[1]) / 1024**2
    except (OSError, ValueError, AttributeError):
        return None


def _gpus() -> list[str]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def report() -> list[str]:
    from pai.config import ConfigError, load_settings

    ram = _ram_gb()
    gpus = _gpus()
    disk = shutil.disk_usage(PROJECT_ROOT)
    lines = [
        "=== Компьютер ===",
        f"Система: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()}",
        f"Процессор: {os.cpu_count()} потоков",
        f"Оперативная память: {ram:.1f} ГБ" if ram else "Оперативная память: не удалось определить",
        "Видеокарта NVIDIA: " + ("; ".join(gpus) if gpus else "не найдена"),
        f"Свободно на диске: {disk.free / 1024**3:.0f} ГБ",
        f"ffmpeg (нужен для голоса): {'есть' if shutil.which('ffmpeg') else 'нет'}",
        "",
        "=== Что потянет этот компьютер ===",
    ]
    big_gpu = any("MiB" in g and int(g.rsplit(",", 1)[1].split()[0]) >= 8000 for g in gpus)
    if ram and ram >= 8:
        lines.append("• Локальное распознавание речи (faster-whisper small) — да, на процессоре.")
    else:
        lines.append("• Локальное распознавание речи — лучше облачное (мало памяти).")
    if big_gpu:
        lines.append("• Локальные модели Ollama 7–8B — да, на видеокарте.")
    elif ram and ram >= 16:
        lines.append("• Локальные модели Ollama 7–8B — запустятся, но медленно (без видеокарты).")
    else:
        lines.append("• Локальные модели Ollama — не рекомендуется, используйте облачные модели.")
    lines += ["", "=== Настройки бота ==="]
    try:
        s = load_settings()
        lines.append(f"Белый список: {len(s.allowed_telegram_ids)} ID")
        for name, ok in (
            ("OpenAI", s.provider_available("openai")),
            ("Claude", s.provider_available("anthropic")),
            ("Ollama", s.provider_available("ollama")),
        ):
            lines.append(f"{name}: {'задан' if ok else 'нет'}")
        lines.append(f"Бюджет в месяц: ${s.monthly_budget_usd}")
        lines.append("Настройки в порядке.")
    except ConfigError as exc:
        lines.append(f"Ошибка настройки: {exc}")
    return lines


def main() -> None:
    print("\n".join(report()))
