"""Подготовка текста ответа к озвучке: без разметки, ссылок и эмодзи, с естественными паузами."""

from __future__ import annotations

import re

_URL = re.compile(r"https?://\S+|www\.\S+")
_MARKDOWN = re.compile(r"[*_`#>|~]+")
_BULLET = re.compile(r"^\s*(?:[-•–—]|\d+[.)])\s+", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0001f900-\U0001f9ff️‍]+"
)
_SPACES = re.compile(r"[ \t]+")


def prepare_for_speech(text: str) -> str:
    """Делает текст пригодным для синтеза: ссылки не зачитываются посимвольно,
    пункты списков и абзацы становятся паузами, разметка и эмодзи убираются."""
    t = _URL.sub("ссылка в тексте сообщения", text)
    t = _EMOJI.sub("", t)
    t = _BULLET.sub("", t)
    t = _MARKDOWN.sub("", t)
    lines = [ln.strip() for ln in t.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if ln[-1] not in ".!?…:;,":
            ln += "."  # конец строки или пункта — пауза
        out.append(ln)
    return _SPACES.sub(" ", " ".join(out)).strip()


def truncate_for_speech(text: str, max_chars: int) -> tuple[str, bool]:
    """Обрезает по границе предложения. Возвращает (текст, был_ли_обрезан)."""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("… "))
    if end > max_chars // 3:
        cut = cut[: end + 1]
    return cut.rstrip(), True
