"""voice settings

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("voice_mode", sa.String(length=10), server_default="auto", nullable=False)
        )
        batch_op.add_column(sa.Column("tts_voice", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("tts_speed", sa.Double(), server_default="1.0", nullable=False))
        batch_op.add_column(
            sa.Column("tts_style", sa.String(length=20), server_default="natural", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("tts_style")
        batch_op.drop_column("tts_speed")
        batch_op.drop_column("tts_voice")
        batch_op.drop_column("voice_mode")
