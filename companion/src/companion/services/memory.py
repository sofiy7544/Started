"""Долгосрочная память: сжатие старой переписки в резюме и извлечение фактов о пользователе."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from companion.config import Settings
from companion.llm.client import LLMClient, LLMError, MemoryExtraction
from companion.llm.prompts import MEMORY_EXTRACTION_SYSTEM, format_transcript
from companion.repositories import memory, users
from companion.repositories.memory import Fact, StoredMessage
from companion.services.budget import BudgetGuard, BudgetLevel
from companion.services.sensitive import is_sensitive, redact

log = logging.getLogger(__name__)

# Пространство имён advisory-блокировок суммаризации (второй ключ — users.id).
_LOCK_NS = 7_041
MAX_NEW_FACTS_PER_RUN = 6
MAX_FACT_CHARS = 200
MAX_NAME_CHARS = 40
MEMORY_MAX_TOKENS = 1500
SUMMARIES_IN_CONTEXT = 4


@dataclass(frozen=True)
class MemorySnapshot:
    facts: list[Fact]
    summaries_count: int
    messages_count: int


class MemoryService:
    def __init__(
        self, pool: asyncpg.Pool, llm: LLMClient, budget: BudgetGuard, settings: Settings,
        persona_name: str,
    ) -> None:
        self._pool = pool
        self._llm = llm
        self._budget = budget
        self._settings = settings
        self._persona_name = persona_name

    async def snapshot(self, user_id: int) -> MemorySnapshot:
        facts = await memory.list_facts(self._pool, user_id)
        summaries = await memory.count_summaries(self._pool, user_id)
        messages = await self._pool.fetchval(
            "SELECT count(*) FROM messages WHERE user_id = $1", user_id
        )
        return MemorySnapshot(facts, summaries, messages)

    async def recent_summaries(self, user_id: int) -> list[str]:
        return await memory.recent_summaries(self._pool, user_id, SUMMARIES_IN_CONTEXT)

    def _pick_batch(self, msgs: list[StoredMessage], boundary: int) -> list[StoredMessage]:
        """Что сжимать: всё до границы /reset + всё старше последних KEEP_RECENT при переполнении."""
        before = [m for m in msgs if m.id <= boundary]
        current = [m for m in msgs if m.id > boundary]
        batch = list(before)
        if len(current) >= self._settings.summarize_threshold:
            batch += current[: len(current) - self._settings.keep_recent_messages]
        return batch

    async def maybe_summarize(self, user_id: int) -> bool:
        """Сжимает старую часть переписки, если пора. True — если резюме сделано."""
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock($1, $2::int)", _LOCK_NS, user_id
            )
            if not locked:
                return False  # уже сжимается другим обработчиком
            user = await users.get(conn, user_id)
            if user is None:
                return False
            batch = self._pick_batch(
                await memory.unsummarized_messages(conn, user_id), user.context_from_message_id
            )
            if not batch:
                return False
            if len(batch) < 2:
                await memory.mark_summarized(conn, user_id, batch[-1].id)
                return False

            if await self._budget.level() is BudgetLevel.EXHAUSTED:
                return False
            known = await memory.list_facts(conn, user_id)
            transcript = format_transcript(batch, known, self._persona_name)
            model = self._settings.memory_model
            if not await self._budget.can_afford(
                model, input_chars=len(transcript) + len(MEMORY_EXTRACTION_SYSTEM),
                max_output_tokens=MEMORY_MAX_TOKENS,
            ):
                return False
            try:
                extraction, resp = await self._llm.extract_memory(
                    model=model, system=MEMORY_EXTRACTION_SYSTEM, transcript=transcript,
                    max_tokens=MEMORY_MAX_TOKENS,
                )
            except LLMError as exc:
                # Не помечаем сжатым — попробуем при следующем сообщении; новых повторов тут нет.
                log.warning("memory extraction failed for user=%s: %s", user_id, exc)
                return False
            await self._budget.record(resp, operation="memory", user_id=user_id)

            if extraction is not None:
                await self._apply(conn, user_id, extraction, known)
                await memory.add_summary(
                    conn, user_id, redact(extraction.summary.strip())[:1000],
                    batch[0].id, batch[-1].id,
                )
            else:
                # Отказ или обрыв: всё равно помечаем, чтобы не тратить деньги на повтор того же.
                log.warning("memory extraction returned nothing for user=%s", user_id)
            await memory.mark_summarized(conn, user_id, batch[-1].id)
            await memory.delete_old_summarized(
                conn, user_id, self._settings.message_retention_days
            )
            return extraction is not None

    async def _apply(
        self, conn: asyncpg.Connection, user_id: int, ex: MemoryExtraction, known: list[Fact]
    ) -> None:
        known_ids = {f.id for f in known}
        # Модель могла вернуть любые числа — удаляем только факты из переданного ей списка
        # (а SQL дополнительно ограничен user_id).
        obsolete = [i for i in ex.obsolete_fact_ids if i in known_ids]
        await memory.remove_facts(conn, user_id, obsolete)

        if ex.preferred_name:
            name = " ".join(ex.preferred_name.split())[:MAX_NAME_CHARS]
            if name and not is_sensitive(name):
                await memory.set_preferred_name(conn, user_id, name)

        new: list[tuple[str, str]] = []
        for item in ex.new_facts[:MAX_NEW_FACTS_PER_RUN]:
            text = " ".join(item.text.split())
            if not text or len(text) > MAX_FACT_CHARS or is_sensitive(text):
                continue
            new.append((item.category, text))
        if new:
            await memory.add_facts(
                conn, user_id, new,
                max_facts=self._settings.max_facts_per_user,
                max_threads=self._settings.max_open_threads,
            )
