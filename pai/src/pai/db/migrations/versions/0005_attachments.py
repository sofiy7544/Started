"""attachments (multimodal)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("fmt", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("facts", sa.Text(), nullable=False),
        sa.Column("problems", sa.Text(), nullable=False),
        sa.Column("meta", sa.Text(), nullable=False),
        sa.Column("stored_path", sa.String(length=300), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachments_conv", "attachments", ["conversation_id", "id"])
    op.create_index("ix_attachments_user", "attachments", ["user_id", "created_at"])
    op.create_table(
        "attachment_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attachment_id", "idx", name="uq_chunk_idx"),
    )


def downgrade() -> None:
    op.drop_table("attachment_chunks")
    op.drop_index("ix_attachments_user", table_name="attachments")
    op.drop_index("ix_attachments_conv", table_name="attachments")
    op.drop_table("attachments")
