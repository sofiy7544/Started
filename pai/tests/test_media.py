"""Вложения: тип по содержимому, защита, разбор документов, изображения, видео, создание файлов,
хранилище. Без сети и без платных вызовов."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from pai.llm.engine import load_history, strip_binary
from pai.media import documents, outputs, safety, video
from pai.media.documents import Section, make_chunks, parse_document, parse_zip
from pai.media.images import prepare_image
from pai.media.ocr import render_pdf_pages
from pai.media.safety import UnsafeFile, sniff
from pai.media.store import AttachmentStore, NewAttachment
from pai.voice.audio import ogg_chunks
from tests import media_samples as ms
from tests.conftest import CLOCK, OWNER

# ---- тип по содержимому и защита -------------------------------------------------------------


def test_type_is_detected_by_content_not_name() -> None:
    assert sniff(ms.pdf_with_text([["Hello"]]), "report.txt") == "pdf"
    assert sniff(ms.docx_file("T", ["p"]), "a.zip") == "docx"
    assert sniff(ms.xlsx_file({"S": [[1]]}), "x") == "xlsx"
    assert sniff(ms.pptx_file([("T", "b", "")]), "p.pptx") == "pptx"
    assert sniff(ms.image_with_text("x", fmt="PNG"), "photo.jpg") == "png"
    assert sniff(b"name;age\nAnna;30\n", "people.csv") == "csv"
    assert sniff("Привет".encode(), "notes") == "txt"


@pytest.mark.parametrize("data,name", [
    (b"MZ\x90\x00" + b"\x00" * 100, "invoice.pdf"),  # программа под видом PDF
    (b"\x7fELF" + b"\x00" * 60, "photo.jpg"),
    (b"\x00\x01\x02binary", "data.bin"),
    (b"Rar!\x1a\x07\x00", "archive.rar"),
    (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "IMG_1.heic"),
])
def test_executables_and_unsupported_files_are_rejected(data: bytes, name: str) -> None:
    with pytest.raises(UnsafeFile):
        sniff(data, name)


def test_dangerous_names() -> None:
    assert safety.is_blocked_name("report.pdf.exe")
    assert safety.is_blocked_name("macro.xlsm")
    assert not safety.is_blocked_name("report.pdf")
    assert safety.safe_name("../../etc/passwd") == "passwd"
    assert safety.safe_name('C:\\Users\\a\\план "Q4".docx') == "план _Q4_.docx"


def test_zip_bomb_is_rejected() -> None:
    bomb = ms.zip_file({"zeros.txt": b"0" * (20 * 1024 * 1024)})
    assert len(bomb) < 100_000
    with pytest.raises(UnsafeFile, match="бомб"):
        parse_zip(bomb)


def test_zip_contents_are_checked_one_by_one() -> None:
    data = ms.zip_file({
        "docs/plan.docx": ms.docx_file("План", ["Запуск в марте"]),
        "photo.png": ms.image_with_text("hi", fmt="PNG"),
        "setup.exe": b"MZ" + b"\x00" * 100,
        "fake.pdf": b"MZ" + b"\x00" * 100,
        "inner.zip": ms.zip_file({"a.txt": b"a"}),
        "../evil.txt": b"x",
        "__MACOSX/._plan.docx": b"junk",
    })
    z = parse_zip(data)
    assert sorted(m.fmt for m in z.members) == ["docx", "png"]
    reasons = "\n".join(z.skipped)
    assert "setup.exe" in reasons and "fake.pdf" in reasons and "inner.zip" in reasons and "evil.txt" in reasons
    assert "__MACOSX" not in reasons


def test_xml_external_entities_are_refused() -> None:
    xxe = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    with pytest.raises(UnsafeFile, match="внешние сущности"):
        parse_document(xxe, "xml")
    ok = parse_document(b"<order><id>7</id></order>", "xml")
    assert "<order>" in ok.sections[0].text and any("корневой элемент <order>" in f for f in ok.facts)


# ---- разбор документов -------------------------------------------------------------------------


def test_pdf_text_and_scanned_pages() -> None:
    doc = parse_document(ms.pdf_with_text([["Invoice 4521", "Total 980 UAH"], ["Page two text here ok"]]), "pdf")
    assert doc.pages == 2 and doc.scanned_pages == []
    assert "Invoice 4521" in doc.sections[0].text and doc.sections[1].title == "стр. 2"
    scan = parse_document(ms.scanned_pdf("Счёт 777"), "pdf")
    assert scan.scanned_pages == [1] and "скан" in "; ".join(scan.facts)
    pages = render_pdf_pages(ms.scanned_pdf("Счёт 777"), [1, 5])
    assert [n for n, _ in pages] == [1] and Image.open(io.BytesIO(pages[0][1])).format == "JPEG"


def test_docx_xlsx_pptx() -> None:
    d = parse_document(ms.docx_file("Отчёт", ["Выручка выросла на 12%."], [["Месяц", "Сумма"], ["Март", "100"]]), "docx")
    text = d.sections[0].text
    assert "## Отчёт" in text and "Выручка выросла" in text and "Март | 100" in text
    x = parse_document(ms.xlsx_file({"Продажи": [["Товар", "Цена"], ["Чай", 12.5]], "Пусто": []}), "xlsx")
    assert x.sections[0].title == "лист «Продажи»" and "Чай | 12.5" in x.sections[0].text
    assert "лист «Пусто»: пустой" in x.facts
    p = parse_document(ms.pptx_file([("Цели", "Рост 20%", "сказать про бюджет")]), "pptx")
    assert p.pages == 1 and "Рост 20%" in p.sections[0].text and "сказать про бюджет" in p.sections[0].text


def test_text_formats_and_encodings() -> None:
    csv_doc = parse_document("Имя;Возраст\nАнна;30\nОлег;41\n".encode("cp1251"), "csv")
    assert "Анна;30" in csv_doc.sections[0].text
    assert any("строк 3 × столбцов 2" in f for f in csv_doc.facts)
    bad = parse_document(b'{"a": 1,,}', "json")
    assert bad.problems and "строка 1" in bad.problems[0]
    good = parse_document('{"имя": "Анна"}'.encode(), "json")
    assert '"имя": "Анна"' in good.sections[0].text


def test_chunks_keep_titles_and_size() -> None:
    sections = [Section(f"стр. {i}", f"Текст страницы {i}. " * 60) for i in range(1, 8)]
    chunks = make_chunks(sections, size=2500)
    assert all(len(c.text) <= 2600 for c in chunks)
    assert chunks[0].title.startswith("стр. 1") and "стр. 7" in chunks[-1].title
    assert "".join(c.text for c in chunks).count("Текст страницы") == 7 * 60
    long = make_chunks([Section("текст", "x" * 9000)], size=4000)
    assert [c.title for c in long] == ["текст, ч. 1", "текст, ч. 2", "текст, ч. 3"]


def test_huge_document_is_capped_honestly(monkeypatch) -> None:
    monkeypatch.setattr(documents, "MAX_DOC_CHARS", 5000)
    doc = parse_document(("строка текста\n" * 2000).encode(), "txt")
    assert doc.text_chars <= 5000 and "прочитаны первые" in doc.problems[0]


# ---- изображения -------------------------------------------------------------------------------


def test_image_is_downscaled_and_metadata_removed() -> None:
    im = Image.new("RGB", (4000, 3000), "white")
    exif = Image.Exif()
    exif[0x8825] = {2: (50, 27, 0)}  # GPS
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    out = prepare_image(buf.getvalue())
    assert (out.width, out.height) == (2048, 1536) and (out.original_width, out.original_height) == (4000, 3000)
    assert not Image.open(io.BytesIO(out.data)).getexif()  # геометка не уходит провайдеру


def test_image_bomb_and_broken_image_rejected() -> None:
    buf = io.BytesIO()
    Image.new("1", (9000, 9000)).save(buf, "PNG")
    with pytest.raises(UnsafeFile, match="слишком большое"):
        prepare_image(buf.getvalue())
    with pytest.raises(UnsafeFile, match="повреждён"):
        prepare_image(b"\xff\xd8\xff" + b"garbage" * 10)


# ---- видео и звук ----------------------------------------------------------------------------


def test_video_frames_at_times_and_audio_in_parts(tmp_path) -> None:
    path = tmp_path / "v.mp4"
    path.write_bytes(ms.video_file(seconds=6))
    info = video.probe(str(path))
    assert 5.9 < info.duration < 6.2 and info.has_audio and (info.width, info.height) == (320, 240)
    frames = video.frames_at(str(path), video.sample_times(info.duration, 3))
    assert [round(t) for t, _ in frames] == [1, 3, 5]
    # Кадр на 5-й секунде — действительно с 5-й секунды (цвет фона меняется каждую секунду).
    px = Image.open(io.BytesIO(frames[-1][1])).getpixel((5, 5))
    assert abs(px[0] - (60 * 5) % 255) < 25
    parts = ogg_chunks(str(path), chunk_seconds=2.5)
    assert len(parts) == 3 and parts[0][0] == 0 and abs(parts[-1][1] - 6.0) < 0.2
    assert video.mmss(75) == "01:15" and video.mmss(3725) == "1:02:05"


def test_broken_video_rejected(tmp_path) -> None:
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    with pytest.raises(UnsafeFile):
        video.probe(str(p))


# ---- создание файлов ---------------------------------------------------------------------------


def test_output_files() -> None:
    from openpyxl import load_workbook

    x = outputs.build_file("итог.xlsx", "### Лист: Март\nТовар;Сумма\nЧай;12,5\n### Лист: Апрель\nТовар,Сумма\nКофе,7\n")
    wb = load_workbook(io.BytesIO(x.data))
    assert wb.sheetnames == ["Март", "Апрель"] and wb["Март"]["B2"].value == 12.5 and wb["Апрель"]["A2"].value == "Кофе"
    d = outputs.build_file("отчёт.docx", "# Итоги\n- пункт\nТекст **важно**\nA | B\n--- | ---\n1 | 2")
    parsed = parse_document(d.data, "docx")
    assert "## Итоги" in parsed.sections[0].text and "1 | 2" in parsed.sections[0].text
    with pytest.raises(outputs.OutputError, match="JSON"):
        outputs.build_file("a.json", "{bad")
    with pytest.raises(outputs.OutputError, match="не поддерживается"):
        outputs.build_file("run.exe", "x")
    assert outputs.build_file("t.csv", "a,b\n1,2").data.startswith("\ufeff".encode())


# ---- история без картинок ----------------------------------------------------------------------


def test_images_are_not_kept_in_history() -> None:
    msgs = [ModelRequest(parts=[UserPromptPart(content=["Что это?", BinaryContent(b"\xff\xd8", media_type="image/jpeg",
                                                                                   identifier="att-7")])]),
            ModelResponse(parts=[TextPart("Это чек.")])]
    from pydantic_ai import ModelMessagesTypeAdapter

    blob = ModelMessagesTypeAdapter.dump_json(strip_binary(msgs))
    assert b"/9j" not in blob  # base64 картинки нет
    restored = load_history([blob])
    assert "вложения #7" in restored[0].parts[0].content[1]


# ---- хранилище ---------------------------------------------------------------------------------


async def test_store_isolation_retention_and_wipe(sessions, assistant, tmp_path) -> None:
    store = AttachmentStore(sessions, tmp_path / "media", retention_hours=24, now=lambda: CLOCK.now)
    owner = await assistant.get_user(OWNER)
    other = await assistant.get_user(222)
    v = await store.add(owner.id, NewAttachment(kind="image", fmt="jpg", name="a.jpg", size_bytes=3, sha256="x",
                                                files={"image.jpg": b"img"}, meta={"file": "image.jpg"},
                                                chunks=[Section("t", "текст")]))
    assert await store.read_file(v, "image.jpg") == b"img"
    assert await store.get(other.id, v.id) is None  # чужое — не видно даже по номеру
    assert await store.read_file(v, "../secrets") is None
    CLOCK.advance(hours=25)
    assert await store.cleanup() == 1
    after = await store.get(owner.id, v.id)
    assert after is not None and not after.files_available and (await store.chunks(owner.id, v.id))[0].text == "текст"
    assert not (tmp_path / "media" / f"u{owner.id}" / f"a{v.id}").exists()
    await store.wipe_user(owner.id)
    assert await store.get(owner.id, v.id) is None
    _ = timedelta
