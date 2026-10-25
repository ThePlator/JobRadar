"""Every per-user row says whose it is (SaaS PR 5b): status history, alerts and resumes get
`user_id`, and one user's notes about jobs move from `setting` to `user_settings`. Existing
rows are the owner's.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREFIXES = ("closing_reminder:", "company_replied:", "followed_up:", "followup_reminder:",
            "skill_gaps_week")  # fmt: skip


def _owned(table: str) -> None:
    """user_id, filled with the owner for every existing row, then required."""
    op.add_column(table, sa.Column("user_id", sa.String(), nullable=True))
    op.execute(f"UPDATE {table} SET user_id = 'owner'")  # noqa: S608 (table names from above)
    op.alter_column(table, "user_id", nullable=False)
    op.create_foreign_key(f"{table}_user_id_fkey", table, "users", ["user_id"], ["id"])


def upgrade() -> None:
    for table in ("artifact", "status_change", "alert"):
        _owned(table)
    op.create_index("artifact_user_job", "artifact", ["user_id", "job_id"])
    op.create_index("status_change_user", "status_change", ["user_id", "changed_at"])
    op.drop_constraint("alert_pkey", "alert", type_="primary")
    op.create_primary_key("alert_pkey", "alert", ["user_id", "job_id"])

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "key"),
    )
    for prefix in PREFIXES:
        like = prefix.replace("_", "\\_") + "%"
        op.execute(
            sa.text(
                "INSERT INTO user_settings (user_id, key, value) SELECT 'owner', key, value "
                "FROM setting WHERE key LIKE :p"
            ).bindparams(p=like)
        )
        op.execute(sa.text("DELETE FROM setting WHERE key LIKE :p").bindparams(p=like))


def downgrade() -> None:
    raise NotImplementedError("rows of several users can't be folded back into one")
