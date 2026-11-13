"""training_people.excluded

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13 13:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("training_people") as batch:
        batch.add_column(sa.Column("excluded", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("training_people") as batch:
        batch.drop_column("excluded")
