"""OCR — распознавание текста там, где его нет в виде текста: фото, скриншоты, сканы PDF.

Как устроено (и почему так):
* у PDF/DOCX/XLSX/PPTX текст извлекается локально и бесплатно (documents.py) — модель не нужна;
* страницы PDF без текстового слоя (сканы) рендерятся в изображения и читаются мультимодальной
  моделью — современные модели распознают печатный и рукописный текст, таблицы и чеки лучше
  классического OCR и понимают смысл (сумма, дата, ошибка в расчёте);
* локальный Tesseract не используется: на Windows его нужно ставить отдельно, а для русского и
  украинского текста на фото и скриншотах он заметно хуже.
"""

from __future__ import annotations

OCR_INSTRUCTIONS = (
    "Чтение текста с изображений: если просят «прочитай», «перепиши», «что написано» — переписывай "
    "дословно, сохраняя порядок, абзацы и таблицы (таблицы — через « | »). Неразборчивое помечай "
    "[неразборчиво], не додумывай. Цифры, суммы, даты и номера переписывай особенно внимательно."
)


def render_pdf_pages(data: bytes, pages: list[int], *, max_side: int = 1600) -> list[tuple[int, bytes]]:
    """Страницы PDF (номера с 1) → JPEG. Без текста: только картинка страницы."""
    import pypdfium2 as pdfium

    from pai.media.images import pil_to_jpeg

    out: list[tuple[int, bytes]] = []
    pdf = pdfium.PdfDocument(data)
    try:
        n = len(pdf)
        for number in pages:
            if not 1 <= number <= n:
                continue
            page = pdf[number - 1]
            try:
                w, h = page.get_size()
                scale = max(0.5, min(3.0, max_side / max(w, h, 1)))
                bitmap = page.render(scale=scale)
                try:
                    out.append((number, pil_to_jpeg(bitmap.to_pil(), max_side)))
                finally:
                    bitmap.close()
            finally:
                page.close()
    finally:
        pdf.close()
    return out
