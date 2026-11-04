"""Хранилище долгосрочной и эпизодической памяти. Каждая операция ограничена одним user_id."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from pai.db.models import MEMORY_KINDS, Episode, Memory
from pai.security.sensitive import is_sensitive, redact

MAX_CONTENT_CHARS = 300
DUPLICATE_RATIO = 0.88
KIND_TITLES = {
    "preference": "Предпочтения",
    "fact": "Факты",
    "project": "Проекты",
    "agreement": "Договорённости",
    "procedure": "Как выполнять задачи",
}
_KIND_BONUS = {"agreement": 0.5, "preference": 0.4, "procedure": 0.3, "project": 0.3, "fact": 0.0}
_WORD = re.compile(r"\w{3,}", re.UNICODE)


def normalize(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split()).strip(" .!,;")


_DIGITS = re.compile(r"\d+")


def is_duplicate(a: str, b: str) -> bool:
    """Почти одинаковый текст — дубликат, но только если совпадают все числа: «дедлайн 15 ноября»
    и «дедлайн 16 ноября» — разные сведения, а не повтор."""
    na, nb = normalize(a), normalize(b)
    if _DIGITS.findall(na) != _DIGITS.findall(nb):
        return False
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= DUPLICATE_RATIO


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def score(m: Memory, now: datetime) -> float:
    """Ценность воспоминания: важность, уверенность, свежесть, частота упоминаний, тип."""
    age = now - _aware(m.updated_at)
    recency = 1.0 if age < timedelta(days=30) else 0.5 if age < timedelta(days=90) else 0.0
    return m.importance + m.confidence + recency + min(m.mentions, 5) * 0.2 + _KIND_BONUS.get(m.kind, 0)


def fts_query(text_value: str, max_terms: int = 12) -> str | None:
    """Запрос FTS5 по словам сообщения. Окончания слов обрезаются («джазом» → «джаз»),
    чтобы находились формы слова; триграммы требуют не меньше трёх символов."""
    terms: list[str] = []
    for word in _WORD.findall(text_value.lower().replace("ё", "е")):
        if word.isdigit():
            continue
        stem = word[:-2] if len(word) >= 6 else word[:-1] if len(word) == 5 else word
        if len(stem) >= 3 and stem not in terms:
            terms.append(stem)
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:max_terms])


@dataclass(frozen=True)
class SearchResult:
    memories: list[Memory]
    episodes: list[Episode]


class MemoryStore:
    def __init__(self, sessions: async_sessionmaker, now=lambda: datetime.now(UTC)) -> None:  # type: ignore[no-untyped-def]
        self._sessions = sessions
        self._now = now

    # ---- чтение ------------------------------------------------------------------------

    async def active(self, user_id: int) -> list[Memory]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Memory).where(Memory.user_id == user_id, Memory.status == "active").order_by(Memory.id)
            )
            return list(rows)

    async def get(self, user_id: int, memory_id: int) -> Memory | None:
        async with self._sessions() as s:
            return await s.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))

    async def core(self, user_id: int, limit: int) -> list[Memory]:
        now = self._now()
        items = sorted(await self.active(user_id), key=lambda m: score(m, now), reverse=True)
        return items[:limit]

    async def counts(self, user_id: int) -> dict[str, int]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(Memory.kind, func.count())
                    .where(Memory.user_id == user_id, Memory.status == "active")
                    .group_by(Memory.kind)
                )
            ).all()
        return {k: n for k, n in rows}

    async def recent_episodes(self, user_id: int, limit: int) -> list[Episode]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(Episode).where(Episode.user_id == user_id).order_by(Episode.id.desc()).limit(limit)
            )
            return list(rows)

    async def episodes_count(self, user_id: int) -> int:
        async with self._sessions() as s:
            return (
                await s.scalar(select(func.count()).select_from(Episode).where(Episode.user_id == user_id))
                or 0
            )

    async def search(self, user_id: int, query: str, *, memories: int = 8, episodes: int = 3) -> SearchResult:
        q = fts_query(query)
        if q is None:
            return SearchResult([], [])
        async with self._sessions() as s:
            if s.bind.dialect.name == "sqlite":
                mem_ids = [
                    r[0]
                    for r in (
                        await s.execute(
                            text(
                                "SELECT m.id FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                                "WHERE memories_fts MATCH :q AND m.user_id = :u AND m.status = 'active' "
                                "ORDER BY bm25(memories_fts) LIMIT :n"
                            ),
                            {"q": q, "u": user_id, "n": memories},
                        )
                    ).all()
                ]
                ep_ids = [
                    r[0]
                    for r in (
                        await s.execute(
                            text(
                                "SELECT e.id FROM episodes_fts f JOIN episodes e ON e.id = f.rowid "
                                "WHERE episodes_fts MATCH :q AND e.user_id = :u "
                                "ORDER BY bm25(episodes_fts) LIMIT :n"
                            ),
                            {"q": q, "u": user_id, "n": episodes},
                        )
                    ).all()
                ]
            else:  # PostgreSQL и др.: простой поиск по подстрокам (этап 9 — pg_trgm)
                terms = [t.strip('"') for t in q.split(" OR ")]
                mem_ids = list(
                    await s.scalars(
                        select(Memory.id)
                        .where(
                            Memory.user_id == user_id,
                            Memory.status == "active",
                            func.lower(Memory.content).contains(terms[0]),
                        )
                        .limit(memories)
                    )
                )
                ep_ids = list(
                    await s.scalars(
                        select(Episode.id)
                        .where(Episode.user_id == user_id, func.lower(Episode.summary).contains(terms[0]))
                        .limit(episodes)
                    )
                )
            mems = (
                {m.id: m for m in await s.scalars(select(Memory).where(Memory.id.in_(mem_ids)))}
                if mem_ids
                else {}
            )
            eps = (
                {e.id: e for e in await s.scalars(select(Episode).where(Episode.id.in_(ep_ids)))}
                if ep_ids
                else {}
            )
        return SearchResult([mems[i] for i in mem_ids if i in mems], [eps[i] for i in ep_ids if i in eps])

    # ---- запись ------------------------------------------------------------------------

    @staticmethod
    def clean(content: str) -> str | None:
        """Чистит текст воспоминания; None — сохранять нельзя (пусто, слишком длинно, секрет)."""
        value = " ".join(content.split())
        if not value or len(value) > MAX_CONTENT_CHARS or is_sensitive(value):
            return None
        return value

    async def add(
        self,
        user_id: int,
        kind: str,
        content: str,
        *,
        confidence: float = 0.8,
        importance: int = 2,
        source: str = "auto",
    ) -> Memory | None:
        """Добавляет воспоминание или объединяет с почти таким же (дубликат)."""
        value = self.clean(content)
        if value is None or kind not in MEMORY_KINDS:
            return None
        confidence = min(max(confidence, 0.0), 1.0)
        importance = min(max(importance, 1), 3)
        now = self._now()
        async with self._sessions() as s, s.begin():
            same_kind = await s.scalars(
                select(Memory).where(
                    Memory.user_id == user_id, Memory.kind == kind, Memory.status == "active"
                )
            )
            for existing in same_kind:
                if is_duplicate(existing.content, value):
                    existing.mentions += 1
                    existing.confidence = max(existing.confidence, confidence)
                    existing.importance = max(existing.importance, importance)
                    existing.updated_at = now
                    if source == "user":
                        existing.content, existing.source = value, "user"
                    return existing
            memory = Memory(
                user_id=user_id,
                kind=kind,
                content=value,
                confidence=confidence,
                importance=importance,
                source=source,
                created_at=now,
                updated_at=now,
            )
            s.add(memory)
            await s.flush()
            return memory

    async def replace(
        self, user_id: int, old_id: int, content: str, *, confidence: float = 0.9
    ) -> Memory | None:
        """Противоречие или изменение: старое уходит в архив со ссылкой на новое."""
        old = await self.get(user_id, old_id)
        if old is None or old.status != "active":
            return None
        value = self.clean(content)
        if value is None:
            return None
        now = self._now()
        async with self._sessions() as s, s.begin():
            new = Memory(
                user_id=user_id,
                kind=old.kind,
                content=value,
                confidence=confidence,
                importance=old.importance,
                source="auto",
                created_at=now,
                updated_at=now,
            )
            s.add(new)
            await s.flush()
            await s.execute(
                update(Memory)
                .where(Memory.id == old.id, Memory.user_id == user_id)
                .values(status="archived", superseded_by=new.id, updated_at=now)
            )
            return new

    async def archive(self, user_id: int, ids: list[int]) -> int:
        if not ids:
            return 0
        async with self._sessions() as s, s.begin():
            result = await s.execute(
                update(Memory)
                .where(Memory.user_id == user_id, Memory.id.in_(ids), Memory.status == "active")
                .values(status="archived", updated_at=self._now())
            )
            return result.rowcount or 0

    async def edit(self, user_id: int, memory_id: int, content: str) -> Memory | None:
        """Правка владельцем: становится достоверной (confidence 1)."""
        value = self.clean(content)
        if value is None:
            return None
        async with self._sessions() as s, s.begin():
            memory = await s.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
            if memory is None:
                return None
            memory.content, memory.source, memory.confidence = value, "user", 1.0
            memory.updated_at = self._now()
            return memory

    async def delete(self, user_id: int, memory_id: int) -> bool:
        async with self._sessions() as s, s.begin():
            await s.execute(
                update(Memory)
                .where(Memory.user_id == user_id, Memory.superseded_by == memory_id)
                .values(superseded_by=None)
            )
            result = await s.execute(delete(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
            return bool(result.rowcount)

    async def touch(self, user_id: int, ids: list[int]) -> None:
        if ids:
            async with self._sessions() as s, s.begin():
                await s.execute(
                    update(Memory)
                    .where(Memory.user_id == user_id, Memory.id.in_(ids))
                    .values(last_used_at=self._now())
                )

    async def enforce_limit(self, user_id: int, max_active: int) -> int:
        """Память не растёт бесконтрольно: сверх лимита в архив уходят наименее ценные."""
        items = await self.active(user_id)
        if len(items) <= max_active:
            return 0
        now = self._now()
        items.sort(key=lambda m: score(m, now))
        return await self.archive(user_id, [m.id for m in items[: len(items) - max_active]])

    async def add_episode(
        self,
        user_id: int,
        conversation_id: int | None,
        summary: str,
        decisions: list[str],
        open_items: list[str],
        first_turn_id: int,
        last_turn_id: int,
    ) -> Episode | None:
        summary = redact(" ".join(summary.split()))[:1000]
        if not summary:
            return None
        clean = lambda items: "\n".join(redact(" ".join(i.split()))[:300] for i in items if i.strip()) or None  # noqa: E731
        async with self._sessions() as s, s.begin():
            ep = Episode(
                user_id=user_id,
                conversation_id=conversation_id,
                summary=summary,
                decisions=clean(decisions),
                open_items=clean(open_items),
                first_turn_id=first_turn_id,
                last_turn_id=last_turn_id,
                created_at=self._now(),
            )
            s.add(ep)
            await s.flush()
            return ep

    async def clear(self, user_id: int) -> None:
        """Удаляет всю память (факты и эпизоды) пользователя."""
        async with self._sessions() as s, s.begin():
            await s.execute(update(Memory).where(Memory.user_id == user_id).values(superseded_by=None))
            await s.execute(delete(Memory).where(Memory.user_id == user_id))
            await s.execute(delete(Episode).where(Episode.user_id == user_id))

    async def export(self, user_id: int) -> dict:
        async with self._sessions() as s:
            mems = list(await s.scalars(select(Memory).where(Memory.user_id == user_id).order_by(Memory.id)))
            eps = list(
                await s.scalars(select(Episode).where(Episode.user_id == user_id).order_by(Episode.id))
            )
        iso = lambda d: _aware(d).isoformat() if d else None  # noqa: E731
        return {
            "exported_at": self._now().isoformat(),
            "memories": [
                {
                    "id": m.id,
                    "kind": m.kind,
                    "content": m.content,
                    "confidence": m.confidence,
                    "importance": m.importance,
                    "mentions": m.mentions,
                    "status": m.status,
                    "superseded_by": m.superseded_by,
                    "source": m.source,
                    "created_at": iso(m.created_at),
                    "updated_at": iso(m.updated_at),
                }
                for m in mems
            ],
            "episodes": [
                {
                    "id": e.id,
                    "summary": e.summary,
                    "decisions": e.decisions,
                    "open_items": e.open_items,
                    "created_at": iso(e.created_at),
                }
                for e in eps
            ],
        }
