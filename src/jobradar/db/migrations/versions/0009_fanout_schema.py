"""Followed sources, per-user tasks, and partial uniqueness (SaaS PR 8a).

Each user follows catalog sources (user_sources). Tasks can be per-user (user_id set)
or catalog-wide (user_id null). Row-level security protects both for scoped requests.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MINE = "user_id = current_setting('app.user_id', true)"


def upgrade() -> None:
    # 1. user_sources table
    op.create_table(
        "user_sources",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("followed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"]),
        sa.PrimaryKeyConstraint("user_id", "source_id"),
    )
    op.create_index(
        "user_sources_by_source", "user_sources", ["source_id", "user_id"], unique=False
    )

    # 2. task table: user_id and partial unique indexes
    op.add_column("task", sa.Column("user_id", sa.String(), nullable=True))
    op.create_foreign_key("task_user_id_fkey", "task", "users", ["user_id"], ["id"])
    op.drop_constraint("task_type_key_key", "task", type_="unique")
    op.create_index(
        "task_catalog_unique",
        "task",
        ["type", "key"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
        sqlite_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "task_user_unique",
        "task",
        ["type", "key", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index("task_ready_user", "task", ["status", "run_after", "user_id"], unique=False)

    # 3. Backfill: every active user follows every approved catalog source
    op.execute(
        "INSERT INTO user_sources (user_id, source_id, followed_at) "
        "SELECT u.id, s.id, now() "
        "FROM users u "
        "CROSS JOIN source s "
        "WHERE s.approval = 'approved' AND u.deleted_at IS NULL "
        "ON CONFLICT DO NOTHING"
    )

    # 4. Row-level security for <database>_app
    op.execute("ALTER TABLE user_sources ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON user_sources USING ({MINE}) WITH CHECK ({MINE})")
    op.execute("ALTER TABLE task ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON task USING ({MINE}) WITH CHECK ({MINE})")


def downgrade() -> None:
    # 1. Drop RLS policies
    op.execute("DROP POLICY own_rows ON task")
    op.execute("ALTER TABLE task DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY own_rows ON user_sources")
    op.execute("ALTER TABLE user_sources DISABLE ROW LEVEL SECURITY")

    # 2. Revert task table: purge per-user tasks so (type, key) constraint can be restored
    op.execute("DELETE FROM task WHERE user_id IS NOT NULL")
    op.drop_index("task_ready_user", table_name="task")
    op.drop_index("task_user_unique", table_name="task")
    op.drop_index("task_catalog_unique", table_name="task")
    op.create_unique_constraint("task_type_key_key", "task", ["type", "key"])
    op.drop_constraint("task_user_id_fkey", "task", type_="foreignkey")
    op.drop_column("task", "user_id")

    # 3. Drop user_sources table
    op.drop_index("user_sources_by_source", table_name="user_sources")
    op.drop_table("user_sources")
