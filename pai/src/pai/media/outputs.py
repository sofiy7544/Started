"""Создание новых файлов по просьбе владельца: отчёт в Word, таблица в Excel, CSV, JSON, текст.

Модель передаёт содержимое текстом, файл собирает код: так формат всегда корректный.
* .xlsx / .csv — содержимое в виде CSV (разделитель «,», «;» или табуляция); несколько листов —
  строки «### Лист: Название» перед каждой таблицей;
* .docx / .md / .txt — текст; в .docx строки «# », «## » становятся заголовками, «- » — списком,
  строки с « | » подряд — таблицей;
* .json — должен быть корректным JSON (иначе — ошибка с объяснением, модель исправит).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

from pai.media.safety import extension, safe_name

OUTPUT_FORMATS = ("txt", "md", "csv", "json", "xlsx", "docx")
MAX_OUTPUT_CHARS = 400_000
SHEET_RE = re.compile(r"^#{2,3}\s*(?:Лист|Sheet)\s*:\s*(.+)$", re.IGNORECASE)


class OutputError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratedFile:
    filename: str
    data: bytes


def _rows(text: str) -> list[list[str]]:
    sample = text[:5000]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim) if row]


def _number(value: str) -> object:
    v = value.replace(" ", "").replace(" ", "")
    if re.fullmatch(r"-?\d+", v) and len(v) < 16 and not (len(v) > 1 and v.lstrip("-").startswith("0")):
        return int(v)
    if re.fullmatch(r"-?\d+[.,]\d+", v):
        return float(v.replace(",", "."))
    return value


def _sheets(content: str) -> list[tuple[str, str]]:
    sheets: list[tuple[str, list[str]]] = []
    for line in content.splitlines():
        m = SHEET_RE.match(line.strip())
        if m:
            sheets.append((m.group(1).strip()[:31] or f"Лист{len(sheets) + 1}", []))
        else:
            if not sheets:
                sheets.append(("Лист1", []))
            sheets[-1][1].append(line)
    return [(name, "\n".join(lines).strip()) for name, lines in sheets if "\n".join(lines).strip()]


def build_xlsx(content: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for name, body in _sheets(content) or [("Лист1", "")]:
        clean = re.sub(r"[\[\]:*?/\\]", "_", name) or "Лист"
        while clean in used:
            clean = clean[:28] + "_" + str(len(used))
        used.add(clean)
        ws = wb.create_sheet(clean)
        rows = _rows(body)
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                cell = ws.cell(row=r, column=c, value=_number(value) if r > 1 else value)
                if r == 1:
                    cell.font = Font(bold=True)
        for c in range(1, max((len(r) for r in rows), default=0) + 1):
            width = max((len(str(r[c - 1])) for r in rows if len(r) >= c), default=8)
            ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 8), 60)
        if rows:
            ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_docx(content: str) -> bytes:
    import docx

    d = docx.Document()
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if " | " in line or line.startswith("|"):
            block = []
            while i < len(lines) and (" | " in lines[i] or lines[i].startswith("|")):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    block.append(cells)
                i += 1
            cols = max(len(r) for r in block)
            table = d.add_table(rows=len(block), cols=cols)
            table.style = "Table Grid"
            for r, row in enumerate(block):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
                    if r == 0:
                        for run in table.cell(r, c).paragraphs[0].runs:
                            run.bold = True
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            d.add_heading(m.group(2).strip(), level=len(m.group(1)))
        elif re.match(r"^\s*[-•*]\s+", line):
            d.add_paragraph(re.sub(r"^\s*[-•*]\s+", "", line), style="List Bullet")
        elif re.match(r"^\s*\d+[.)]\s+", line):
            d.add_paragraph(re.sub(r"^\s*\d+[.)]\s+", "", line), style="List Number")
        elif line.strip():
            p = d.add_paragraph()
            # **жирный** — простая разметка, которую модели пишут по привычке.
            for k, part in enumerate(re.split(r"\*\*(.+?)\*\*", line)):
                if part:
                    p.add_run(part).bold = k % 2 == 1
        i += 1
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def build_file(filename: str, content: str) -> GeneratedFile:
    name = safe_name(filename, "result.txt")
    fmt = extension(name)
    if fmt not in OUTPUT_FORMATS:
        raise OutputError(f"формат .{fmt or '?'} не поддерживается; можно: {', '.join(OUTPUT_FORMATS)}")
    if not content.strip():
        raise OutputError("пустое содержимое")
    if len(content) > MAX_OUTPUT_CHARS:
        raise OutputError("слишком большой файл")
    if fmt == "xlsx":
        data = build_xlsx(content)
    elif fmt == "docx":
        data = build_docx(content)
    elif fmt == "json":
        try:
            data = json.dumps(json.loads(content), ensure_ascii=False, indent=2).encode("utf-8")
        except json.JSONDecodeError as exc:
            raise OutputError(f"некорректный JSON: строка {exc.lineno}, позиция {exc.colno}") from exc
    elif fmt == "csv":
        out = io.StringIO()
        csv.writer(out, delimiter=";").writerows(_rows(content))
        data = ("﻿" + out.getvalue()).encode("utf-8")  # BOM — Excel на Windows откроет кириллицу
    else:
        data = content.encode("utf-8")
    return GeneratedFile(name, data)
