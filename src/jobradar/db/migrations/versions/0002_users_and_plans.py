"""Users and plans, for signing in (SaaS PR 4). Plan rows are added by `init_db`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("llm_calls_per_day", sa.Integer(), nullable=False),
        sa.Column("resume_builds_per_day", sa.Integer(), nullable=False),
        sa.Column("emails_per_day", sa.Integer(), nullable=False),
        sa.Column("followed_sources_max", sa.Integer(), nullable=True),
        sa.Column("api_tokens_max", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("auth_uid", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.String(), nullable=True),
        sa.CheckConstraint("role IN ('user', 'admin')", name="users_role"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth_uid"),
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_table("plans")
