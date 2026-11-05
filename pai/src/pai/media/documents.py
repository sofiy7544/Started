"""Document Parser: текст и структура документов — локально и бесплатно, без отправки файла модели.

PDF (pypdfium2 — движок PDF из Chrome), DOCX (python-docx), XLSX (openpyxl, только значения —
формулы и макросы не выполняются), PPTX (python-pptx), CSV, TXT/MD/LOG, JSON, XML (defusedxml —
защита от XML-бомб и внешних сущностей), ZIP (содержимое проверяется и разбирается по файлам).

Результат — разделы (страница, лист, слайд…) и честные заметки: что прочитано, что нет.
Функции синхронные: движок вызывает их в отдельном потоке.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time

from pai.media.safety import (
    DOCUMENT_FORMATS,
    IMAGE_FORMATS,
    UnsafeFile,
    ZipLimits,
    bad_member_path,
    check_zip,
    extension,
    is_blocked_name,
    looks_like_text,
    read_member,
    safe_name,
    sniff,
)

MAX_DOC_CHARS = 600_000  # ≈ 300 страниц; дальше — с пометкой «прочитано не всё»
MAX_PDF_PAGES = 500
MAX_SHEET_ROWS = 5_000
MAX_SHEET_COLS = 100
MAX_ZIP_MEMBERS = 50
SCANNED_PAGE_CHARS = 25  # меньше символов на странице — скорее всего скан без текстового слоя


@dataclass
class Section:
    title: str
    text: str


@dataclass
class ParsedDocument:
    fmt: str
    sections: list[Section] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)  # «12 стр.», «3 листа»…
    problems: list[str] = field(default_factory=list)  # что прочитать не удалось
    pages: int | None = None
    scanned_pages: list[int] = field(default_factory=list)  # номера страниц PDF без текста (с 1)

    @property
    def text_chars(self) -> int:
        return sum(len(s.text) for s in self.sections)


@dataclass
class ZipMember:
    name: str
    fmt: str
    data: bytes


@dataclass
class ParsedZip:
    members: list[ZipMember] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # «virus.exe — исполняемый файл»
    total: int = 0


# ---- текст ---------------------------------------------------------------------------------


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(data[:200_000]).best()
    if best is not None and best.encoding:
        return data.decode(best.encoding, errors="replace")
    return data.decode("cp1251", errors="replace")


def _cap(doc: ParsedDocument) -> ParsedDocument:
    """Ограничение объёма текста — с честной пометкой."""
    total = 0
    kept: list[Section] = []
    for s in doc.sections:
        if total + len(s.text) > MAX_DOC_CHARS:
            rest = MAX_DOC_CHARS - total
            if rest > 500:
                kept.append(Section(s.title, s.text[:rest]))
            doc.problems.append(f"документ очень большой — прочитаны первые ≈{MAX_DOC_CHARS // 1000} тыс. символов")
            break
        kept.append(s)
        total += len(s.text)
    doc.sections = kept
    return doc


def parse_text(data: bytes, fmt: str) -> ParsedDocument:
    text = decode_text(data).replace("\r\n", "\n")
    doc = ParsedDocument(fmt)
    if fmt == "json":
        try:
            value = json.loads(text)
            text = json.dumps(value, ensure_ascii=False, indent=1)
            doc.facts.append("JSON корректный")
        except json.JSONDecodeError as exc:
            doc.problems.append(f"JSON с ошибкой (строка {exc.lineno}, позиция {exc.colno}) — показан как текст")
        except RecursionError:
            doc.problems.append("JSON слишком глубоко вложенный — показан как текст")
    elif fmt == "xml":
        import defusedxml.ElementTree as DET
        from defusedxml import DefusedXmlException

        try:
            root = DET.fromstring(data)
            doc.facts.append(f"XML корректный, корневой элемент <{root.tag.split('}')[-1]}>")
        except DefusedXmlException as exc:
            raise UnsafeFile("XML содержит внешние сущности или DTD — не обрабатываю (защита от атак)") from exc
        except Exception:  # noqa: BLE001 — ParseError и т. п.: показываем как текст
            doc.problems.append("XML с ошибкой разметки — показан как текст")
    elif fmt in ("csv", "tsv"):
        _csv_facts(text, doc)
    if fmt not in ("csv", "tsv"):
        doc.facts.append(f"строк: {len(text.splitlines())}")
    doc.sections.append(Section("текст", text))
    return _cap(doc)


def _csv_facts(text: str, doc: ParsedDocument) -> None:
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rows = 0
    cols = 0
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        rows += 1
        cols = max(cols, len(row))
        if rows > 1_000_000:
            break
    shown = {"\t": "табуляция"}.get(delim, delim)
    doc.facts.append(f"таблица: строк {rows} × столбцов {cols}, разделитель «{shown}»")


# ---- PDF -----------------------------------------------------------------------------------


def parse_pdf(data: bytes) -> ParsedDocument:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise UnsafeFile("PDF защищён паролем — пришлите без пароля") from exc
        raise UnsafeFile("PDF повреждён — не удалось открыть") from exc
    doc = ParsedDocument("pdf")
    try:
        n = len(pdf)
        doc.pages = n
        doc.facts.append(f"{n} стр.")
        limit = min(n, MAX_PDF_PAGES)
        for i in range(limit):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    text = tp.get_text_bounded().replace("\r\n", "\n").strip()
                finally:
                    tp.close()
                # Скан: текста почти нет, а на странице есть изображение.
                scanned = len(text) < SCANNED_PAGE_CHARS and _has_image(page)
            finally:
                page.close()
            if scanned:
                doc.scanned_pages.append(i + 1)
            if text:
                doc.sections.append(Section(f"стр. {i + 1}", text))
        if n > limit:
            doc.problems.append(f"прочитаны первые {limit} из {n} страниц")
    finally:
        pdf.close()
    if doc.scanned_pages:
        if len(doc.scanned_pages) == doc.pages:
            doc.facts.append("текстового слоя нет (скан)")
        else:
            doc.facts.append(f"страниц без текстового слоя: {len(doc.scanned_pages)}")
    else:
        doc.facts.append("текст прочитан")
    return _cap(doc)


def _has_image(page) -> bool:  # type: ignore[no-untyped-def]
    import pypdfium2.raw as pdfium_c

    return any(obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE for obj in page.get_objects(max_depth=2))


# ---- офисные документы ---------------------------------------------------------------------


def _office_zip_check(data: bytes) -> list[str]:
    notes = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        check_zip(z, ZipLimits(max_entries=5000, max_total_bytes=300 * 1024 * 1024, max_member_bytes=150 * 1024 * 1024))
        names = z.namelist()
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            notes.append("в файле есть макросы — они не запускались")
        media = [n for n in names if "/media/" in n and extension(n) in ("png", "jpeg", "jpg", "gif", "emf", "wmf", "bmp", "tiff")]
        if media:
            notes.append(f"внутри изображений: {len(media)} — их содержимое не анализировалось, только текст")
    return notes


def parse_docx(data: bytes) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    notes = _office_zip_check(data)
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — повреждённый файл
        raise UnsafeFile("DOCX повреждён — не удалось открыть") from exc
    doc = ParsedDocument("docx")
    lines: list[str] = []
    tables = 0
    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith(("heading", "заголовок", "title")):
                lines.append(f"\n## {text}")
            elif style.lower().startswith(("list", "спис")):
                lines.append(f"• {text}")
            else:
                lines.append(text)
        elif tag == "tbl":
            tables += 1
            t = Table(el, d)
            lines.append(f"\n[таблица {tables}]")
            for row in t.rows:
                cells = []
                for c in row.cells:
                    v = " ".join(c.text.split())
                    if not cells or cells[-1] != v:  # объединённые ячейки повторяются
                        cells.append(v)
                lines.append(" | ".join(cells))
            lines.append("")
    text = "\n".join(lines).strip()
    words = len(text.split())
    doc.facts.append(f"≈{words} слов" + (f", таблиц: {tables}" if tables else ""))
    doc.problems.extend(n for n in notes if "изображ" in n)
    doc.facts.extend(n for n in notes if "макрос" in n)
    if not text:
        doc.problems.append("текста в документе нет")
    else:
        doc.sections.append(Section("документ", text))
    return _cap(doc)


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.10g}"
    return " ".join(str(v).split())


def parse_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    notes = _office_zip_check(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise UnsafeFile("XLSX повреждён — не удалось открыть") from exc
    doc = ParsedDocument("xlsx")
    try:
        sheets = wb.worksheets
        doc.facts.append(f"листов: {len(sheets)}")
        for ws in sheets:
            rows: list[str] = []
            count = 0
            truncated = False
            for values in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in values[:MAX_SHEET_COLS]]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    continue
                count += 1
                if count > MAX_SHEET_ROWS:
                    truncated = True
                    break
                rows.append(" | ".join(cells))
            title = f"лист «{ws.title}»"
            if truncated:
                doc.problems.append(f"{title}: прочитаны первые {MAX_SHEET_ROWS} непустых строк")
            if rows:
                doc.sections.append(Section(title, "\n".join(rows)))
                doc.facts.append(f"{title}: строк {min(count, MAX_SHEET_ROWS)}")
            else:
                doc.facts.append(f"{title}: пустой")
    finally:
        wb.close()
    doc.facts.extend(notes)
    doc.facts.append("формулы показаны значениями, сохранёнными в файле")
    return _cap(doc)


def parse_pptx(data: bytes) -> ParsedDocument:
    from pptx import Presentation

    notes = _office_zip_check(data)
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise UnsafeFile("PPTX повреждён — не удалось открыть") from exc
    doc = ParsedDocument("pptx")
    slides = list(prs.slides)
    doc.pages = len(slides)
    doc.facts.append(f"слайдов: {len(slides)}")
    for i, slide in enumerate(slides, 1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                t = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if t.strip():
                    parts.append(t.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(" ".join(c.text.split()) for c in row.cells))
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if note:
                parts.append(f"[заметки докладчика] {note}")
        if parts:
            doc.sections.append(Section(f"слайд {i}", "\n".join(parts)))
    doc.facts.extend(notes)
    if not doc.sections:
        doc.problems.append("текста на слайдах нет")
    return _cap(doc)


PARSERS = {"pdf": parse_pdf, "docx": parse_docx, "xlsx": parse_xlsx, "pptx": parse_pptx}


def parse_document(data: bytes, fmt: str) -> ParsedDocument:
    if fmt in PARSERS:
        return PARSERS[fmt](data)
    if fmt in ("txt", "md", "log", "csv", "tsv", "json", "xml"):
        return parse_text(data, fmt)
    raise UnsafeFile("этот тип документа не поддерживаю")


# ---- ZIP -----------------------------------------------------------------------------------


def parse_zip(data: bytes) -> ParsedZip:
    """Содержимое архива: подходящие файлы (документы и изображения) — для разбора по одному,
    остальное — в список пропущенных с причиной. Вложенные архивы не раскрываются."""
    result = ParsedZip()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeFile("архив повреждён") from exc
    with z:
        check_zip(z)
        files = [i for i in z.infolist() if not i.is_dir()]
        result.total = len(files)
        for info in files:
            name = info.filename
            shown = safe_name(name)
            if bad_member_path(name):
                result.skipped.append(f"{shown} — подозрительный путь в архиве")
                continue
            if name.startswith("__MACOSX/") or shown.startswith("._") or shown in ("Thumbs.db", ".DS_Store"):
                continue
            if is_blocked_name(shown):
                result.skipped.append(f"{shown} — исполняемый/опасный тип файла")
                continue
            if info.flag_bits & 0x1:
                result.skipped.append(f"{shown} — зашифрован паролем")
                continue
            if len(result.members) >= MAX_ZIP_MEMBERS:
                result.skipped.append(f"{shown} — больше {MAX_ZIP_MEMBERS} файлов в архиве не разбираю")
                continue
            try:
                body = read_member(z, info, ZipLimits().max_member_bytes)
                fmt = sniff(body, shown)
            except UnsafeFile as exc:
                result.skipped.append(f"{shown} — {exc}")
                continue
            if fmt == "zip":
                result.skipped.append(f"{shown} — вложенный архив не раскрываю")
                continue
            if fmt not in DOCUMENT_FORMATS and fmt not in IMAGE_FORMATS:
                result.skipped.append(f"{shown} — аудио/видео внутри архива не обрабатываю")
                continue
            result.members.append(ZipMember(name=name[-200:], fmt=fmt, data=body))
    return result


# ---- деление на части ----------------------------------------------------------------------


def make_chunks(sections: list[Section], size: int = 4000) -> list[Section]:
    """Части для модели и поиска: соседние разделы склеиваются, длинные режутся по абзацам."""
    pieces: list[Section] = []
    for s in sections:
        text = s.text.strip()
        if not text:
            continue
        if len(text) <= size:
            pieces.append(Section(s.title, text))
            continue
        buf: list[str] = []
        length = 0
        part = 1
        for para in _paragraphs(text, size):
            if buf and length + len(para) > size:
                pieces.append(Section(f"{s.title}, ч. {part}", "\n".join(buf)))
                part += 1
                buf, length = [], 0
            buf.append(para)
            length += len(para) + 1
        if buf:
            pieces.append(Section(f"{s.title}, ч. {part}" if part > 1 else s.title, "\n".join(buf)))
    merged: list[Section] = []
    first_title = ""
    for p in pieces:
        if merged and len(merged[-1].text) + len(p.text) + 2 <= size:
            last = merged[-1]
            merged[-1] = Section(f"{first_title} — {p.title}", f"{last.text}\n\n[{p.title}]\n{p.text}")
        else:
            first_title = p.title
            merged.append(Section(p.title, f"[{p.title}]\n{p.text}" if len(pieces) > 1 else p.text))
    return merged


def _paragraphs(text: str, size: int) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        while len(line) > size:  # строка без переносов (минифицированный JSON и т. п.)
            cut = line.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def is_supported_text(data: bytes) -> bool:
    return looks_like_text(data)
