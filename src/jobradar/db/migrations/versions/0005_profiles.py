"""Each user's profile and preferences (SaaS PR 5c). The owner's are copied in from
profile.yaml and config.yaml on start, and again whenever those files change.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, column in (("profiles", "profile_json"), ("preferences", "preferences_json")):
        op.create_table(
            table,
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column(column, sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("user_id"),
        )


def downgrade() -> None:
    op.drop_table("preferences")
    op.drop_table("profiles")
