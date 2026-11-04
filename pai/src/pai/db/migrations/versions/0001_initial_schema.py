"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-04 22:14:53.895069
"""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index('ix_audit_created', ['created_at'], unique=False)

    op.create_table('processed_updates',
    sa.Column('update_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('update_id')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_id', sa.BigInteger(), nullable=False),
    sa.Column('first_name', sa.String(length=200), nullable=True),
    sa.Column('model_id', sa.String(length=100), nullable=True),
    sa.Column('instructions', sa.Text(), nullable=True),
    sa.Column('reply_style', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tg_id')
    )
    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index('ix_conversations_user_open', ['user_id', 'closed_at'], unique=False)

    op.create_table('usage_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('model_id', sa.String(length=100), nullable=False),
    sa.Column('operation', sa.String(length=30), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('confirmed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('usage_records', schema=None) as batch_op:
        batch_op.create_index('ix_usage_created', ['created_at'], unique=False)

    op.create_table('turns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('user_text', sa.Text(), nullable=False),
    sa.Column('assistant_text', sa.Text(), nullable=False),
    sa.Column('messages', sa.LargeBinary(), nullable=False),
    sa.Column('model_id', sa.String(length=100), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('turns', schema=None) as batch_op:
        batch_op.create_index('ix_turns_conversation', ['conversation_id', 'id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('turns', schema=None) as batch_op:
        batch_op.drop_index('ix_turns_conversation')

    op.drop_table('turns')
    with op.batch_alter_table('usage_records', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_created')

    op.drop_table('usage_records')
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index('ix_conversations_user_open')

    op.drop_table('conversations')
    op.drop_table('users')
    op.drop_table('processed_updates')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_created')

    op.drop_table('audit_events')
