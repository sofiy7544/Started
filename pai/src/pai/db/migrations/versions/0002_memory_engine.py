"""memory engine

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04 22:37:39.411664
"""
import sqlalchemy as sa
from alembic import op

# Полнотекстовый поиск по памяти: SQLite FTS5 с триграммами (работает с кириллицей,
# находит части слов). Таблицы-индексы синхронизируются триггерами. На PostgreSQL поиск
# выполняется без FTS5 (см. pai.memory.store), эти объекты не создаются.
_FTS = [
    ("memories_fts", "memories", "content"),
    ("episodes_fts", "episodes", "summary"),
]


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('memories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Double(), nullable=False),
    sa.Column('importance', sa.Integer(), nullable=False),
    sa.Column('mentions', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('superseded_by', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['superseded_by'], ['memories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.create_index('ix_memories_user_status', ['user_id', 'status', 'kind'], unique=False)

    op.create_table('episodes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('decisions', sa.Text(), nullable=True),
    sa.Column('open_items', sa.Text(), nullable=True),
    sa.Column('first_turn_id', sa.Integer(), nullable=False),
    sa.Column('last_turn_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.create_index('ix_episodes_user', ['user_id', 'id'], unique=False)

    with op.batch_alter_table('turns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('memory_processed', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.create_index('ix_turns_memory_pending', ['user_id', 'memory_processed'], unique=False)


    if op.get_bind().dialect.name == "sqlite":
        for fts, table, column in _FTS:
            op.execute(f"CREATE VIRTUAL TABLE {fts} USING fts5({column}, content='{table}', "
                       f"content_rowid='id', tokenize='trigram')")
            op.execute(f"CREATE TRIGGER {table}_ai AFTER INSERT ON {table} BEGIN "
                       f"INSERT INTO {fts}(rowid, {column}) VALUES (new.id, new.{column}); END")
            delete_old = f"INSERT INTO {fts}({fts}, rowid, {column}) VALUES ('delete', old.id, old.{column});"
            op.execute(f"CREATE TRIGGER {table}_ad AFTER DELETE ON {table} BEGIN {delete_old} END")
            op.execute(f"CREATE TRIGGER {table}_au AFTER UPDATE OF {column} ON {table} BEGIN {delete_old} "
                       f"INSERT INTO {fts}(rowid, {column}) VALUES (new.id, new.{column}); END")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for fts, table, _column in _FTS:
            for suffix in ("ai", "ad", "au"):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix}")
            op.execute(f"DROP TABLE IF EXISTS {fts}")
    with op.batch_alter_table('turns', schema=None) as batch_op:
        batch_op.drop_index('ix_turns_memory_pending')
        batch_op.drop_column('memory_processed')

    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.drop_index('ix_episodes_user')

    op.drop_table('episodes')
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.drop_index('ix_memories_user_status')

    op.drop_table('memories')
