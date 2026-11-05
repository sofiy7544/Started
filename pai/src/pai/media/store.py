"""Хранение вложений: описание и текст (по частям) — в базе, файлы (изображения, PDF для
просмотра страниц, видео для кадров) — в media_dir/u<пользователь>/a<вложение>/ на ограниченное
время. После срока файлы удаляются, извлечённый текст остаётся с разговором.

Каждый запрос ограничен user_id: чужое вложение недоступно даже по номеру.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import Attachment, AttachmentChunk
from pai.media.documents import Section

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttachmentView:
    id: int
    user_id: int
    conversation_id: int | None
    parent_id: int | None
    kind: str
    fmt: str
    name: str
    size_bytes: int
    status: str
    facts: list[str]
    problems: list[str]
    meta: dict
    chunk_count: int
    text_chars: int
    stored_path: str | None
    created_at: datetime

    @property
    def files_available(self) -> bool:
        return self.stored_path is not None


@dataclass
class NewAttachment:
    kind: str
    fmt: str
    name: str
    size_bytes: int
    sha256: str
    status: str = "ok"
    facts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    chunks: list[Section] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)  # имя → содержимое
    move_files: dict[str, Path] = field(default_factory=dict)  # имя → временный файл (видео)
    parent_id: int | None = None


def _view(a: Attachment) -> AttachmentView:
    created = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=UTC)
    return AttachmentView(
        id=a.id, user_id=a.user_id, conversation_id=a.conversation_id, parent_id=a.parent_id, kind=a.kind,
        fmt=a.fmt, name=a.name, size_bytes=a.size_bytes, status=a.status,
        facts=[f for f in a.facts.split("\n") if f], problems=[p for p in a.problems.split("\n") if p],
        meta=json.loads(a.meta or "{}"), chunk_count=a.chunk_count, text_chars=a.text_chars,
        stored_path=a.stored_path, created_at=created,
    )


class AttachmentStore:
    def __init__(
        self,
        sessions: async_sessionmaker,
        media_dir: Path,
        *,
        retention_hours: float = 24,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self.media_dir = media_dir
        self.tmp_dir = media_dir / "tmp"
        self._retention = timedelta(hours=retention_hours)
        self._now = now

    # ---- запись -------------------------------------------------------------------------

    async def add(self, user_id: int, new: NewAttachment) -> AttachmentView:
        has_files = bool(new.files or new.move_files)
        async with self._sessions() as s, s.begin():
            row = Attachment(
                user_id=user_id, parent_id=new.parent_id, kind=new.kind, fmt=new.fmt, name=new.name[:200],
                size_bytes=new.size_bytes, sha256=new.sha256, status=new.status,
                facts="\n".join(new.facts), problems="\n".join(new.problems),
                meta=json.dumps(new.meta, ensure_ascii=False), chunk_count=len(new.chunks),
                text_chars=sum(len(c.text) for c in new.chunks), created_at=self._now(),
                file_expires_at=self._now() + self._retention if has_files else None,
            )
            s.add(row)
            await s.flush()
            for i, c in enumerate(new.chunks, 1):
                s.add(AttachmentChunk(attachment_id=row.id, idx=i, title=c.title[:200], text=c.text))
            if has_files:
                rel = f"u{user_id}/a{row.id}"
                await asyncio.to_thread(self._write_files, self.media_dir / rel, new.files, new.move_files)
                row.stored_path = rel
            await s.flush()
            return _view(row)

    @staticmethod
    def _write_files(folder: Path, files: dict[str, bytes], move: dict[str, Path]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (folder / name).write_bytes(data)
        for name, src in move.items():
            shutil.move(str(src), folder / name)

    async def link(self, user_id: int, ids: list[int], conversation_id: int) -> None:
        if not ids:
            return
        async with self._sessions() as s, s.begin():
            await s.execute(
                update(Attachment)
                .where(Attachment.user_id == user_id, Attachment.id.in_(ids))
                .values(conversation_id=conversation_id)
            )
            # Файлы из архива — вместе с архивом.
            await s.execute(
                update(Attachment)
                .where(Attachment.user_id == user_id, Attachment.parent_id.in_(ids))
                .values(conversation_id=conversation_id)
            )

    async def set_notes(self, user_id: int, attachment_id: int, idx: int, notes: str) -> None:
        async with self._sessions() as s, s.begin():
            owned = await s.scalar(
                select(Attachment.id).where(Attachment.id == attachment_id, Attachment.user_id == user_id)
            )
            if owned is None:
                return
            await s.execute(
                update(AttachmentChunk)
                .where(AttachmentChunk.attachment_id == attachment_id, AttachmentChunk.idx == idx)
                .values(notes=notes)
            )

    # ---- чтение -------------------------------------------------------------------------

    async def get(self, user_id: int, attachment_id: int) -> AttachmentView | None:
        async with self._sessions() as s:
            row = await s.scalar(
                select(Attachment).where(Attachment.id == attachment_id, Attachment.user_id == user_id)
            )
            return _view(row) if row else None

    async def many(self, user_id: int, ids: list[int]) -> list[AttachmentView]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Attachment).where(Attachment.user_id == user_id, Attachment.id.in_(ids)).order_by(Attachment.id)
            )
            return [_view(r) for r in rows]

    async def for_conversation(self, user_id: int, conversation_id: int, limit: int = 30) -> list[AttachmentView]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Attachment)
                .where(Attachment.user_id == user_id, Attachment.conversation_id == conversation_id)
                .order_by(Attachment.id.desc())
                .limit(limit)
            )
            return list(reversed([_view(r) for r in rows]))

    async def chunks(self, user_id: int, attachment_id: int) -> list[AttachmentChunk]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(AttachmentChunk)
                .join(Attachment, Attachment.id == AttachmentChunk.attachment_id)
                .where(Attachment.user_id == user_id, Attachment.id == attachment_id)
                .order_by(AttachmentChunk.idx)
            )
            return list(rows)

    async def chunks_of_conversation(self, user_id: int, conversation_id: int) -> list[tuple[Attachment, AttachmentChunk]]:
        async with self._sessions() as s:
            rows = await s.execute(
                select(Attachment, AttachmentChunk)
                .join(AttachmentChunk, Attachment.id == AttachmentChunk.attachment_id)
                .where(Attachment.user_id == user_id, Attachment.conversation_id == conversation_id)
                .order_by(Attachment.id, AttachmentChunk.idx)
            )
            return [(a, c) for a, c in rows.all()]

    def path(self, view: AttachmentView, name: str) -> Path | None:
        if view.stored_path is None or "/" in name or "\\" in name or name.startswith("."):
            return None
        p = self.media_dir / view.stored_path / name
        return p if p.is_file() else None

    async def read_file(self, view: AttachmentView, name: str) -> bytes | None:
        p = self.path(view, name)
        return await asyncio.to_thread(p.read_bytes) if p else None

    # ---- очистка ------------------------------------------------------------------------

    async def cleanup(self) -> int:
        """Удаляет файлы с истёкшим сроком хранения (текст в базе остаётся) и старые временные."""
        now = self._now()
        async with self._sessions() as s, s.begin():
            rows = (
                await s.execute(
                    select(Attachment.id, Attachment.stored_path).where(
                        Attachment.stored_path.is_not(None), Attachment.file_expires_at <= now
                    )
                )
            ).all()
            if rows:
                await s.execute(
                    update(Attachment)
                    .where(Attachment.id.in_([r[0] for r in rows]))
                    .values(stored_path=None, file_expires_at=None)
                )
        for _id, rel in rows:
            await asyncio.to_thread(shutil.rmtree, self.media_dir / rel, True)
        await asyncio.to_thread(self._clean_tmp, now)
        if rows:
            log.info("media cleanup: removed files of %d attachments", len(rows))
        return len(rows)

    def _clean_tmp(self, now: datetime) -> None:
        if not self.tmp_dir.is_dir():
            return
        border = now.timestamp() - 3600
        for p in self.tmp_dir.iterdir():
            try:
                if p.stat().st_mtime < border:
                    p.unlink()
            except OSError:
                pass

    async def wipe_user(self, user_id: int) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(delete(Attachment).where(Attachment.user_id == user_id))
        await asyncio.to_thread(shutil.rmtree, self.media_dir / f"u{user_id}", True)

    async def run_forever(self, interval: float = 3600) -> None:
        while True:
            try:
                await self.cleanup()
            except Exception:  # noqa: BLE001 — очистка не должна останавливать бота
                log.exception("media cleanup failed")
            await asyncio.sleep(interval)
