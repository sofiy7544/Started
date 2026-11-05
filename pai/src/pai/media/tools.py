"""Инструменты модели для вложений. Создаются на каждый запрос и привязаны к владельцу и текущему
разговору: открыть можно только своё. Ошибки возвращаются текстом — модель объяснит владельцу."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic_ai import BinaryContent, FunctionToolset, ToolReturn

from pai.media.engine import MediaEngine
from pai.media.ocr import render_pdf_pages
from pai.media.outputs import GeneratedFile, OutputError, build_file
from pai.media.video import mmss

MAX_FILES_PER_ANSWER = 5
SEARCH_RESULTS = 6


@dataclass
class FileEffects:
    """Файлы, созданные за этот ответ, — бот отправит их документами."""

    files: list[GeneratedFile] = field(default_factory=list)


def _terms(query: str) -> list[str]:
    words = re.findall(r"[\w\-]{2,}", query.lower())
    # Грубая «основа» слова: русские/украинские окончания не мешают поиску.
    return [w[:6] if len(w) > 6 else w for w in words][:12]


def build_toolset(media: MediaEngine, user_id: int, conversation_id: int, effects: FileEffects) -> FunctionToolset:
    store = media.store

    async def _own(attachment_id: int):  # type: ignore[no-untyped-def]
        v = await store.get(user_id, attachment_id)
        if v is None or (v.conversation_id not in (None, conversation_id)):
            return None
        return v

    async def view_attachment(attachment_id: int, page: int | None = None) -> ToolReturn | str:
        """Посмотреть изображение вложения ещё раз: фото, страницу PDF (page — номер страницы)
        или ключевые кадры видео. Для текста документов используй read_attachment.

        Args:
            attachment_id: номер вложения (#N из описи)
            page: для PDF — номер страницы (с 1)
        """
        v = await _own(attachment_id)
        if v is None:
            return f"Вложение #{attachment_id} не найдено в этом разговоре."
        if not v.files_available:
            return (f"Файл вложения #{attachment_id} уже удалён (хранится {media.settings.media_retention_hours} ч). "
                    "Доступно только извлечённое содержимое — read_attachment. Скажи владельцу, что для повторного "
                    "просмотра нужно прислать файл снова.")
        if v.kind == "image":
            data = await store.read_file(v, v.meta.get("file", ""))
            if data is None:
                return "Файл изображения не найден."
            return ToolReturn(return_value=f"Изображение #{v.id} «{v.name}» — ниже.",
                              content=[f"Вложение #{v.id}:", BinaryContent(data, media_type=v.meta.get("media_type", "image/jpeg"),
                                                                            identifier=f"att-{v.id}")])
        if v.kind == "video":
            parts: list = []
            for fr in v.meta.get("frames", [])[:8]:
                data = await store.read_file(v, fr["file"])
                if data:
                    parts += [f"Видео #{v.id}, кадр {mmss(fr['t'])}:", BinaryContent(data, media_type="image/jpeg")]
            return ToolReturn(return_value=f"Ключевые кадры видео #{v.id} — ниже.", content=parts) if parts else "Кадров нет."
        if v.fmt == "pdf":
            pdf = await store.read_file(v, v.meta.get("file", ""))
            if pdf is None:
                return "Изображения страниц этого PDF не сохранялись: в нём есть текст — используй read_attachment."
            pages = [page] if page else (v.meta.get("scanned_pages") or [1])[:3]
            import asyncio

            rendered = await asyncio.to_thread(render_pdf_pages, pdf, pages)
            if not rendered:
                return f"Страницы {pages} нет (всего {v.meta.get('pages')})."
            parts = []
            for number, data in rendered:
                parts += [f"Документ #{v.id}, стр. {number}:", BinaryContent(data, media_type="image/jpeg")]
            return ToolReturn(return_value=f"Страницы {[n for n, _ in rendered]} документа #{v.id} — ниже.", content=parts)
        return f"#{v.id} — {v.kind}, у него нет изображения; используй read_attachment."

    async def read_attachment(attachment_id: int, part: int = 1) -> str:
        """Прочитать часть текста вложения: документа, таблицы, презентации или расшифровки речи.

        Args:
            attachment_id: номер вложения (#N из описи)
            part: номер части (с 1); сколько частей — в описи
        """
        v = await _own(attachment_id)
        if v is None:
            return f"Вложение #{attachment_id} не найдено в этом разговоре."
        chunks = await store.chunks(user_id, v.id)
        if not chunks:
            return f"У вложения #{v.id} нет текста" + (" (скан — смотри view_attachment)." if v.meta.get("scanned_pages") else ".")
        if not 1 <= part <= len(chunks):
            return f"Части {part} нет: всего частей {len(chunks)}."
        c = chunks[part - 1]
        return f"Вложение #{v.id} «{v.name}», часть {part} из {len(chunks)} ({c.title}):\n{c.text}"

    async def search_attachments(query: str, attachment_id: int | None = None) -> str:
        """Найти место в текстах вложений этого разговора (документы, таблицы, расшифровки):
        по словам, числам, именам. Возвращает фрагменты с номерами вложений и частей.

        Args:
            query: что искать (несколько ключевых слов или число)
            attachment_id: искать только в этом вложении (необязательно)
        """
        terms = _terms(query)
        if not terms:
            return "Пустой запрос."
        rows = await store.chunks_of_conversation(user_id, conversation_id)
        scored = []
        for a, c in rows:
            if attachment_id is not None and a.id != attachment_id:
                continue
            low = c.text.lower()
            score = sum(low.count(t) for t in terms) + 3 * sum(1 for t in terms if t in low)
            if score:
                pos = min((low.find(t) for t in terms if t in low), default=0)
                snippet = c.text[max(0, pos - 300): pos + 500].replace("\n", " ")
                scored.append((score, a.id, a.name, c.idx, c.title, snippet))
        if not scored:
            return "Ничего не найдено. Попробуй другие слова или read_attachment."
        scored.sort(key=lambda x: -x[0])
        return "\n\n".join(f"#{aid} «{name}», часть {idx} ({title}): …{snip}…"
                           for _s, aid, name, idx, title, snip in scored[:SEARCH_RESULTS])

    async def digest_document(attachment_id: int) -> str:
        """Конспект большого документа по всем частям (для резюме, обзора, поиска противоречий
        по всему тексту). Делается дешёвой моделью один раз и сохраняется.

        Args:
            attachment_id: номер вложения (#N из описи)
        """
        v = await _own(attachment_id)
        if v is None:
            return f"Вложение #{attachment_id} не найдено в этом разговоре."
        return await media.digest(user_id, v)

    async def video_frames(attachment_id: int, start_seconds: float, end_seconds: float, count: int = 6) -> ToolReturn | str:
        """Кадры видео за отрезок времени — чтобы рассмотреть конкретный момент.

        Args:
            attachment_id: номер видео (#N)
            start_seconds: начало отрезка, секунды от начала видео
            end_seconds: конец отрезка, секунды
            count: сколько кадров (1–8)
        """
        v = await _own(attachment_id)
        if v is None or v.kind != "video":
            return f"Видео #{attachment_id} не найдено в этом разговоре."
        if not v.files_available:
            return "Видео уже удалено (срок хранения истёк) — доступны только расшифровка и описание. Попроси прислать снова."
        frames = await media.video_frames(v, start_seconds, end_seconds, count)
        if not frames:
            return "Кадры за этот отрезок получить не удалось."
        parts: list = []
        for t, data in frames:
            parts += [f"Видео #{v.id}, {mmss(t)}:", BinaryContent(data, media_type="image/jpeg")]
        return ToolReturn(return_value=f"Кадры {mmss(start_seconds)}–{mmss(end_seconds)} видео #{v.id} — ниже.", content=parts)

    async def create_file(filename: str, content: str) -> str:
        """Создать файл и отправить владельцу: .docx (отчёт), .xlsx (таблица), .csv, .json, .md, .txt.

        Args:
            filename: имя файла с расширением, например «отчёт.docx» или «сравнение.xlsx»
            content: содержимое. Для .xlsx/.csv — таблица в формате CSV (первая строка — заголовки;
                несколько листов — строка «### Лист: Название» перед каждой таблицей). Для .docx —
                текст: «# »/«## » заголовки, «- » пункты, строки «a | b | c» — таблица.
        """
        if len(effects.files) >= MAX_FILES_PER_ANSWER:
            return f"Не создано: не больше {MAX_FILES_PER_ANSWER} файлов за ответ."
        try:
            f = build_file(filename, content)
        except OutputError as exc:
            return f"Не создано: {exc}. Исправь и вызови снова."
        effects.files.append(f)
        return f"Файл «{f.filename}» создан ({max(1, len(f.data) // 1024)} КБ) и будет отправлен владельцу после ответа."

    return FunctionToolset([view_attachment, read_attachment, search_attachments, digest_document,
                            video_frames, create_file])
