"""Образцы файлов для тестов вложений — создаются кодом, без внешних программ."""

from __future__ import annotations

import io
import zipfile

FONT = None


def _font(size: int):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def image_with_text(text: str, size: tuple[int, int] = (800, 300), color: str = "white", fmt: str = "JPEG") -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    y = 40
    for line in text.split("\n"):
        d.text((30, y), line, fill="black", font=_font(36))
        y += 50
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def pdf_with_text(pages: list[list[str]]) -> bytes:
    """Простой PDF с текстовым слоем (латиница, шрифт Helvetica)."""
    objs: list[str] = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    for lines in pages:
        content = "BT /F1 14 Tf 50 750 Td " + " ".join(f"({ln}) Tj 0 -20 Td" for ln in lines) + " ET"
        page_no = len(objs) + 1
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_no + 1} 0 R "
                    "/Resources << /Font << /F1 FONT 0 R >> >> >>")
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        kids.append(f"{page_no} 0 R")
    font_no = len(objs) + 1
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    objs = [o.replace("FONT 0 R", f"{font_no} 0 R") for o in objs]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def scanned_pdf(text: str) -> bytes:
    """«Скан»: PDF только с картинкой страницы, без текстового слоя."""
    from PIL import Image

    im = Image.open(io.BytesIO(image_with_text(text, (1240, 1754))))
    buf = io.BytesIO()
    im.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def docx_file(title: str, paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx

    d = docx.Document()
    d.add_heading(title, 1)
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, v in enumerate(row):
                t.cell(r, c).text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx_file(sheets: dict[str, list[list[object]]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pptx_file(slides: list[tuple[str, str, str]]) -> bytes:
    """slides: (заголовок, текст, заметки докладчика)."""
    from pptx import Presentation

    prs = Presentation()
    for title, body, notes in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
        if notes:
            s.notes_slide.notes_text_frame.text = notes
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def zip_file(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def video_file(seconds: int = 6, fps: int = 10, audio: bytes | None = None, labels: list[str] | None = None) -> bytes:
    """MP4 (H.264 + AAC): на каждой секунде своя надпись и цвет; звук — тон или переданный звук."""
    import av
    import numpy as np
    from PIL import Image, ImageDraw

    buf = io.BytesIO()
    c = av.open(buf, "w", format="mp4")
    v = c.add_stream("libx264", rate=fps)
    v.width, v.height, v.pix_fmt = 320, 240, "yuv420p"
    a = c.add_stream("aac", rate=48000)
    a.layout = "mono"
    f = _font(32)
    for i in range(seconds * fps):
        sec = i // fps
        im = Image.new("RGB", (320, 240), ((60 * sec) % 255, 90, 160))
        label = labels[sec] if labels and sec < len(labels) else f"сек {sec}"
        ImageDraw.Draw(im).text((30, 100), label, fill="white", font=f)
        for p in v.encode(av.VideoFrame.from_image(im)):
            c.mux(p)
    if audio is None:
        t = np.arange(48000 * seconds) / 48000
        pcm = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32).reshape(1, -1)
    else:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=48000)
        chunks = []
        with av.open(io.BytesIO(audio)) as src:
            for fr in src.decode(audio=0):
                for rf in resampler.resample(fr):
                    chunks.append(rf.to_ndarray().reshape(-1))
        pcm = np.concatenate(chunks)[: 48000 * seconds].astype(np.float32).reshape(1, -1)
    for i in range(0, pcm.shape[1], 1024):
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm[:, i:i + 1024]), format="flt", layout="mono")
        fr.sample_rate = 48000
        for p in a.encode(fr):
            c.mux(p)
    for s in (v, a):
        for p in s.encode(None):
            c.mux(p)
    c.close()
    return buf.getvalue()
