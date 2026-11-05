"""Проверка вложений до разбора: тип — по содержимому (а не по имени), исполняемые файлы не
принимаются, архивы и офисные файлы (это тоже ZIP) проверяются на «бомбы» и подмену путей.

Ничего из присланного не запускается и не распаковывается на диск: файлы читаются в память
с ограничениями размера. Макросы в офисных документах не выполняются (библиотеки их не умеют).
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass

# Расширения, которые никогда не принимаем (в том числе внутри архивов).
BLOCKED_EXTENSIONS = frozenset({
    "exe", "dll", "com", "bat", "cmd", "scr", "msi", "msp", "ps1", "psm1", "vbs", "vbe", "js", "jse",
    "wsf", "wsh", "hta", "cpl", "jar", "apk", "app", "sh", "bash", "lnk", "reg", "pif", "gadget",
    "iso", "img", "dmg", "deb", "rpm", "so", "dylib", "sys", "drv", "ocx", "inf", "scf", "url",
    "docm", "xlsm", "pptm", "dotm", "xltm", "xlam", "ppam",
})

# Форматы: имя → вид вложения.
DOCUMENT_FORMATS = {"pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "json", "xml", "log", "tsv"}
IMAGE_FORMATS = {"jpg", "png", "webp", "gif", "bmp", "tiff"}
AUDIO_FORMATS = {"mp3", "ogg", "m4a", "wav", "flac", "aac", "opus"}
VIDEO_FORMATS = {"mp4", "mov", "webm", "mkv", "avi", "3gp"}
ARCHIVE_FORMATS = {"zip"}
TEXT_EXTENSIONS = {"txt": "txt", "text": "txt", "md": "md", "markdown": "md", "log": "log", "csv": "csv",
                   "tsv": "tsv", "json": "json", "xml": "xml", "ini": "txt", "yaml": "txt", "yml": "txt"}


class UnsafeFile(Exception):
    """Файл отклонён: понятная владельцу причина в тексте исключения."""


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 500
    max_total_bytes: int = 200 * 1024 * 1024  # сумма распакованных размеров
    max_member_bytes: int = 50 * 1024 * 1024
    max_ratio: int = 150  # сжатие сильнее — признак zip-бомбы


def safe_name(name: str | None, fallback: str = "file") -> str:
    """Имя файла для показа и сохранения: без путей, управляющих символов и опасных знаков."""
    raw = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = unicodedata.normalize("NFC", raw)
    raw = "".join(ch for ch in raw if ch.isprintable())
    raw = re.sub(r'[<>:"|?*\x00-\x1f]', "_", raw).strip(" .")
    return (raw or fallback)[:120]


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_blocked_name(name: str) -> bool:
    parts = name.lower().split(".")
    # «report.pdf.exe» и «photo.jpg .scr» — блокируем по последнему расширению.
    return len(parts) > 1 and parts[-1].strip() in BLOCKED_EXTENSIONS


def _zip_kind(head_names: set[str]) -> str:
    if "word/document.xml" in head_names:
        return "docx"
    if "xl/workbook.xml" in head_names:
        return "xlsx"
    if "ppt/presentation.xml" in head_names:
        return "pptx"
    return "zip"


def sniff(data: bytes, name: str) -> str:
    """Формат по первым байтам (а для текста — по расширению). UnsafeFile — если файл
    исполняемый или формат не поддерживается."""
    head = data[:64]
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF") or head[:4] in (
        b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"
    ):
        raise UnsafeFile("исполняемые файлы (программы) не принимаю")
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return _zip_kind(set(z.namelist()[:2000]))
        except zipfile.BadZipFile as exc:
            raise UnsafeFile("архив повреждён") from exc
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    if head.startswith(b"BM") and len(data) > 26:
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "m4a"
        if brand in (b"heic", b"heix", b"mif1", b"heim", b"heis", b"avif"):
            raise UnsafeFile("формат HEIC/AVIF не поддерживаю — пришлите как фото (не файлом) или в JPG")
        if brand.startswith(b"qt"):
            return "mov"
        if brand.startswith(b"3g"):
            return "3gp"
        return "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm" if b"webm" in data[:64] else "mkv"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "aac"
    if head.startswith((b"Rar!", b"7z\xbc\xaf", b"\x1f\x8b", b"BZh")):
        raise UnsafeFile("из архивов поддерживаю только ZIP")
    ext = extension(name)
    if ext in TEXT_EXTENSIONS and looks_like_text(data):
        return TEXT_EXTENSIONS[ext]
    if not ext and looks_like_text(data):
        return "txt"
    if ext in ("doc", "xls", "ppt", "rtf", "odt", "ods", "odp"):
        raise UnsafeFile(f"старый формат .{ext} не поддерживаю — сохраните как .docx/.xlsx/.pptx")
    raise UnsafeFile("этот тип файла не поддерживаю")


def looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True  # UTF-16 с меткой
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return control <= len(sample) // 100


DEFAULT_ZIP_LIMITS = ZipLimits()


def check_zip(z: zipfile.ZipFile, limits: ZipLimits = DEFAULT_ZIP_LIMITS) -> None:
    """Защита от zip-бомб: число файлов, общий и одиночный размер, степень сжатия."""
    infos = z.infolist()
    if len(infos) > limits.max_entries:
        raise UnsafeFile(f"в архиве слишком много файлов ({len(infos)}, максимум {limits.max_entries})")
    total = 0
    for info in infos:
        total += info.file_size
        if info.file_size > limits.max_member_bytes:
            raise UnsafeFile(f"файл в архиве слишком большой после распаковки: {safe_name(info.filename)}")
        if info.compress_size and info.file_size / info.compress_size > limits.max_ratio and info.file_size > 1_000_000:
            raise UnsafeFile("архив похож на «zip-бомбу» (подозрительно сильное сжатие)")
    if total > limits.max_total_bytes:
        raise UnsafeFile("архив слишком большой после распаковки")


def read_member(z: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Чтение файла из архива с жёстким пределом (заголовок с размером можно подделать)."""
    with z.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise UnsafeFile("файл в архиве больше заявленного")
    return data


def bad_member_path(name: str) -> bool:
    n = name.replace("\\", "/")
    return n.startswith("/") or ".." in n.split("/") or (len(n) > 1 and n[1] == ":")
