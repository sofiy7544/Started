"""Схема базы данных. Все пользовательские таблицы привязаны к users.id."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
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
    tts_style: Mapped[str] = mapped_column(
        String(20), default="natural", server_default="natural", nullable=False
    )
    # Часовой пояс владельца: время напоминаний и тихие часы считаются в нём.
    timezone: Mapped[str] = mapped_column(
        String(50), default="Europe/Kyiv", server_default="Europe/Kyiv", nullable=False
    )
    # Инициативные сообщения: выключены, пока владелец не включит (/proactive_on).
    proactive_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )
    proactive_frequency: Mapped[str] = mapped_column(
        String(10), default="normal", server_default="normal", nullable=False
    )  # rare | normal | often
    quiet_start: Mapped[int] = mapped_column(
        Integer, default=22 * 60, server_default=str(22 * 60), nullable=False
    )  # минуты от полуночи, местное время
    quiet_end: Mapped[int] = mapped_column(
        Integer, default=9 * 60, server_default=str(9 * 60), nullable=False
    )
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
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    decisions: Mapped[str | None] = mapped_column(Text)  # по одному на строку
    open_items: Mapped[str | None] = mapped_column(Text)
    first_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_episodes_user", "user_id", "id"),)


REPEAT_KINDS = ("none", "daily", "weekdays", "weekly", "monthly")


class Reminder(Base):
    """Напоминание. next_run_at — следующий момент отправки (UTC); повторы считаются в часовом
    поясе напоминания, чтобы «каждый понедельник в 9:00» не сдвигался при переходе на летнее время."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)  # ЧЧ:ММ — для повторов
    repeat: Mapped[str] = mapped_column(String(10), default="none", nullable=False)
    # Для ежемесячных: исходное число месяца (31-е в феврале → 28-е, но в марте снова 31-е).
    anchor_day: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)  # active|done|cancelled
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_reminders_due", "status", "next_run_at"),
        Index("ix_reminders_user", "user_id"),
    )


class ReminderDelivery(Base):
    """Журнал отправок. UNIQUE(reminder_id, scheduled_for) — одно и то же срабатывание не уйдёт дважды."""

    __tablename__ = "reminder_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="sending", nullable=False)  # sending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("reminder_id", "scheduled_for", name="uq_delivery_once"),)


class ProactiveMessage(Base):
    """Журнал инициативных сообщений: что, когда, о чём (topic) и ответил ли владелец."""

    __tablename__ = "proactive_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="sending", nullable=False)  # sending|sent|failed
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_proactive_user", "user_id", "created_at"),)


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


class Attachment(Base):
    """Вложение (фото, документ, аудио, видео, файл из архива). Текст хранится частями в
    attachment_chunks; сам файл — на диске (media_dir) только временно (file_expires_at)."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # image|document|audio|video|archive
    fmt: Mapped[str] = mapped_column(String(10), nullable=False)  # jpg, pdf, docx…
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="ok", nullable=False)  # ok|partial|failed
    facts: Mapped[str] = mapped_column(Text, default="", nullable=False)  # «12 стр.; текст прочитан»
    problems: Mapped[str] = mapped_column(Text, default="", nullable=False)  # что не удалось
    meta: Mapped[str] = mapped_column(Text, default="{}", nullable=False)  # JSON: страницы, кадры…
    stored_path: Mapped[str | None] = mapped_column(String(300))  # относительно media_dir
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_attachments_conv", "conversation_id", "id"),
        Index("ix_attachments_user", "user_id", "created_at"),
    )


class AttachmentChunk(Base):
    """Часть текста вложения (страницы, лист, слайды, минута расшифровки). notes — краткий
    конспект части дешёвой моделью (кэш: второй раз не платим)."""

    __tablename__ = "attachment_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)  # с 1
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("attachment_id", "idx", name="uq_chunk_idx"),)


class Task(Base):
    """Задача владельца (план работ): раздел, что сделать, кто, когда, статус."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    section: Mapped[str] = mapped_column(String(100), default="Общее", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    person: Mapped[str | None] = mapped_column(String(100))  # кто делает / с кем
    due: Mapped[str | None] = mapped_column(String(50))  # срок как сказал владелец
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)  # open|done|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tasks_user", "user_id", "status"),)


# ---- контроль обучения (рабочая группа). Отдельно от личных данных владельца ----------------


class TrainingPerson(Base):
    """Участник рабочей группы. is_employee — в списке сотрудников; dm_ok — запускал бота в личке."""

    __tablename__ = "training_people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    is_employee: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dm_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Убран владельцем (/remove) — автоматически в список больше не попадает.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingRecord(Base):
    """Запись работы с клиентом. result NULL — подпись не распознана (просили добавить)."""

    __tablename__ = "training_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("training_people.id", ondelete="CASCADE"), nullable=False
    )
    caption_name: Mapped[str | None] = mapped_column(String(100))
    client: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[str | None] = mapped_column(String(12))  # closed|rescheduled|refused|no_answer
    reschedule_text: Mapped[str | None] = mapped_column(String(100))
    reschedule_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    file_id: Mapped[str] = mapped_column(String(300), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    link: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    score: Mapped[float | None] = mapped_column(Float)
    score_comment: Mapped[str | None] = mapped_column(Text)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_training_record_msg"),
        Index("ix_training_records_person", "person_id", "created_at"),
    )


class TrainingAbsence(Base):
    __tablename__ = "training_absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("training_people.id", ondelete="CASCADE"), nullable=False
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingReason(Base):
    """Объяснение сотрудника, почему нет записей («нет клиентов», «болею»)."""

    __tablename__ = "training_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("training_people.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # group|dm
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingJob(Base):
    """Журнал выполненных проверок и отчётов: UNIQUE(kind, key) — после перезапуска ничего не дублируется."""

    __tablename__ = "training_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("kind", "key", name="uq_training_job"),)


class TrainingSetting(Base):
    __tablename__ = "training_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
