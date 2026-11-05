"""reminders and proactive

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05 09:09:32.799085
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proactive_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("proactive_messages", schema=None) as batch_op:
        batch_op.create_index("ix_proactive_user", ["user_id", "created_at"], unique=False)

    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("repeat", sa.String(length=10), nullable=False),
        sa.Column("anchor_day", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.create_index("ix_reminders_due", ["status", "next_run_at"], unique=False)
        batch_op.create_index("ix_reminders_user", ["user_id"], unique=False)

    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reminder_id", "scheduled_for", name="uq_delivery_once"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("timezone", sa.String(length=50), server_default="Europe/Kyiv", nullable=False)
        )
        batch_op.add_column(
            sa.Column("proactive_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(
            sa.Column("proactive_frequency", sa.String(length=10), server_default="normal", nullable=False)
        )
        batch_op.add_column(sa.Column("quiet_start", sa.Integer(), server_default="1320", nullable=False))
        batch_op.add_column(sa.Column("quiet_end", sa.Integer(), server_default="540", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("quiet_end")
        batch_op.drop_column("quiet_start")
        batch_op.drop_column("proactive_frequency")
        batch_op.drop_column("proactive_enabled")
        batch_op.drop_column("timezone")

    op.drop_table("reminder_deliveries")
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.drop_index("ix_reminders_user")
        batch_op.drop_index("ix_reminders_due")

    op.drop_table("reminders")
    with op.batch_alter_table("proactive_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_proactive_user")

    op.drop_table("proactive_messages")
