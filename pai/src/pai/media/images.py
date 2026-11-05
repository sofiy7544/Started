"""Image Understanding: подготовка изображения для мультимодальной модели.

* поворот по EXIF, удаление метаданных (геолокация и т. п. не уходят провайдеру);
* уменьшение до разумного размера — модели всё равно сжимают большие картинки, а лишние
  пиксели стоят денег; для мелкого текста (скриншоты, чеки) оставляем до 2048 px;
* защита от «бомб» распаковки (картинка 50 000×50 000 из нескольких КБ).
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from pai.media.safety import UnsafeFile

MAX_PIXELS = 60_000_000  # больше — отклоняем без декодирования
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    original_width: int
    original_height: int


def prepare_image(data: bytes, *, max_side: int = 2048, quality: int = 88) -> PreparedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            if im.width * im.height > MAX_PIXELS:
                raise UnsafeFile("изображение слишком большое")
            im.seek(0)  # анимация (GIF) — первый кадр
            im.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UnsafeFile("изображение слишком большое") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise UnsafeFile("не удалось открыть изображение — файл повреждён") from exc
    ow, oh = im.size
    im = ImageOps.exif_transpose(im)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if has_alpha:
        # Прозрачность (скриншоты, логотипы) — PNG; без метаданных: info не передаём.
        im.convert("RGBA").save(out, "PNG", optimize=True)
        media_type = "image/png"
    else:
        im.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
        media_type = "image/jpeg"
    return PreparedImage(out.getvalue(), media_type, im.width, im.height, ow, oh)


def downscale_jpeg(data: bytes, max_side: int) -> bytes:
    """Готовое изображение — в меньший JPEG (кадры видео, страницы PDF)."""
    im = Image.open(io.BytesIO(data))
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def pil_to_jpeg(im: Image.Image, max_side: int, quality: int = 85) -> bytes:
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()
