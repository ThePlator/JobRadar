"""Catalog sources: approval, where the row came from, and how to read a suggestion
(SaaS PR 7). Rows already there were read by the platform, so they stay approved; they
are history (`seen`), not a watch list, until config.yaml syncs the ones it still lists.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column("approval", sa.String(), nullable=False, server_default="approved"),
    )
    op.add_column("source", sa.Column("origin", sa.String(), nullable=False, server_default="seen"))
    op.add_column("source", sa.Column("suggested_by", sa.String(), nullable=True))
    op.add_column("source", sa.Column("detail_json", postgresql.JSONB(), nullable=True))
    op.add_column(
        "source",
        sa.Column("last_ok_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_foreign_key("source_suggested_by_fkey", "source", "users", ["suggested_by"], ["id"])
    op.create_check_constraint(
        "source_approval", "source", "approval IN ('pending', 'approved', 'rejected')"
    )
    op.create_check_constraint(
        "source_origin", "source", "origin IN ('config', 'suggestion', 'seen')"
    )
    # The model has no server default: inserts set the value. Existing rows are filled.
    op.alter_column("source", "approval", server_default=None)
    op.alter_column("source", "origin", server_default=None)


def downgrade() -> None:
    op.drop_constraint("source_origin", "source", type_="check")
    op.drop_constraint("source_approval", "source", type_="check")
    op.drop_constraint("source_suggested_by_fkey", "source", type_="foreignkey")
    op.drop_column("source", "last_ok_at")
    op.drop_column("source", "detail_json")
    op.drop_column("source", "suggested_by")
    op.drop_column("source", "origin")
    op.drop_column("source", "approval")
