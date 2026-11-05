"""Схема базы данных. Все пользовательские таблицы привязаны к users.id."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(200))
    model_id: Mapped[str | None] = mapped_column(String(100))  # выбранная модель
    instructions: Mapped[str | None] = mapped_column(Text)  # личные инструкции
    reply_style: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    # Голос: auto — отвечать голосом на голосовые; always — всегда; never — только текстом.
    voice_mode: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto", nullable=False)
    tts_voice: Mapped[str | None] = mapped_column(String(60))
    tts_speed: Mapped[float] = mapped_column(default=1.0, server_default="1.0", nullable=False)
    tts_style: Mapped[str] = mapped_column(String(20), default="natural", server_default="natural",
                                           nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    """Разговор: «🆕 Новый диалог» закрывает текущий и начинает следующий."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_conversations_user_open", "user_id", "closed_at"),)


class Turn(Base):
    """Один обмен «сообщение → ответ». messages — сообщения pydantic-ai этого обмена (JSON),
    из них восстанавливается контекст для модели; тексты — для архива и поиска."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    messages: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Обработан ли обмен памятью (извлечены факты и эпизод).
    memory_processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_turns_conversation", "conversation_id", "id"),
        Index("ix_turns_memory_pending", "user_id", "memory_processed"),
    )


MEMORY_KINDS = ("preference", "fact", "project", "agreement", "procedure")


class Memory(Base):
    """Долгосрочная память о владельце. kind:
    preference — предпочтения и стиль; fact — устойчивые факты о жизни и работе;
    project — проекты и их состояние; agreement — договорённости и обещания;
    procedure — как владелец хочет, чтобы выполнялись привычные задачи."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(default=0.8, nullable=False)  # 0..1
    importance: Mapped[int] = mapped_column(Integer, default=2, nullable=False)  # 1..3
    mentions: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|archived
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("memories.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)  # auto|user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_memories_user_status", "user_id", "status", "kind"),)


class Episode(Base):
    """Эпизодическая память: что обсуждали, какие решения приняли, что осталось открытым."""

    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    decisions: Mapped[str | None] = mapped_column(Text)  # по одному на строку
    open_items: Mapped[str | None] = mapped_column(Text)
    first_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_episodes_user", "user_id", "id"),)


class UsageRecord(Base):
    """Учёт расходов на API. confirmed=False — исход неизвестен (таймаут, отмена):
    записана верхняя оценка."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_usage_created", "created_at"),)


class AuditEvent(Base):
    """Журнал действий: что произошло, без текста сообщений."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_audit_created", "created_at"),)


class ProcessedUpdate(Base):
    """Защита от повторной обработки одного и того же апдейта Telegram."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
