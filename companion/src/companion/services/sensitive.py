"""Поиск и маскирование чувствительных данных до сохранения в БД и в память персонажа.

Регулярные выражения ловят типовые форматы (карты, телефоны, e-mail, коды, документы).
Произвольный пароль без пометки распознать нельзя — поэтому модель-экстрактору
дополнительно запрещено сохранять такие сведения.
"""

from __future__ import annotations

import re

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_OTHER: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[e-mail скрыт]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "[счёт скрыт]"),
    # Телефоны: +7 (999) 123-45-67, 8 999 123 45 67, +44 20 7946 0958.
    (
        re.compile(
            r"(?<![\w+])(?:(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}|\+\d[\d\s()-]{8,16}\d)(?!\d)"
        ),
        "[телефон скрыт]",
    ),
    # Коды подтверждения и PIN: только если рядом цифры («код 123456», «cvv: 123»).
    (
        re.compile(r"(?i)\b(код\w*|code|cvv|cvc|pin|пин)(\s+(?:из\s+смс|sms))?\s*[:=\-—]?\s*\d{3,8}\b"),
        r"\1\2 [скрыто]",
    ),
    # Пароли: «пароль: qwerty», «password = 123», «мой пароль qwerty».
    (
        re.compile(r"(?i)\b(парол[ьяе]\w*|password|passwd)(\s*(?:[:=\-—]|от\s+\S+\s*[:=\-—]?|это)\s*|\s+)(\S+)"),
        r"\1\2[скрыто]",
    ),
    # Паспорт РФ: серия и номер (4 + 6 цифр).
    (re.compile(r"(?<!\d)\d{2}\s?\d{2}\s\d{6}(?!\d)"), "[документ скрыт]"),
]

_SENSITIVE_WORDS = re.compile(
    r"(?i)(парол|password|cvv|cvc|пин-?код|код из смс|sms code|паспорт|снилс|\bинн\b|"
    r"номер карт|реквизит|диагноз|\bадрес|живёт по|живет по)"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[номер карты скрыт]" if _luhn_ok(digits) else match.group(0)


def redact(text: str) -> str:
    text = _CARD.sub(_mask_card, text)
    for pattern, replacement in _OTHER:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive(text: str) -> bool:
    """True, если факт нельзя сохранять в долгосрочную память."""
    return redact(text) != text or bool(_SENSITIVE_WORDS.search(text))
