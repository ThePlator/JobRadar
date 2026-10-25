"""Each person's view of a job moves to user_jobs (SaaS PR 5a): score, reason, status, when
they set it, and their Notion page. `job` keeps what is true for everyone. Existing rows
become the owner's, the self-hosted app's one user.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MINE = "('new', 'hidden', 'shortlisted', 'resume_ready', 'applied', 'interview', 'offer', 'rejected', 'skipped')"
CATALOG = "('discovered', 'extracted', 'needs_review', 'discarded')"


def upgrade() -> None:
    op.create_table(
        "user_jobs",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_reason", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("user_status_at", sa.String(), nullable=True),
        sa.Column("notion_page_id", sa.String(), nullable=True),
        sa.Column("notion_synced_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(f"status IN {MINE}", name="user_jobs_status"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "job_id"),
    )
    op.create_index("user_jobs_status", "user_jobs", ["user_id", "status"])
    op.create_index("user_jobs_score", "user_jobs", ["user_id", "score"])
    op.add_column("job", sa.Column("status_reason", sa.String(), nullable=True))

    # The owner, who has every existing score and status (init_db adds the same rows later).
    op.execute(
        "INSERT INTO plans (id, llm_calls_per_day, resume_builds_per_day, emails_per_day, "
        "followed_sources_max, api_tokens_max) VALUES ('free', 100, 5, 20, 50, 2) "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(
        "INSERT INTO users (id, auth_uid, role, plan_id, created_at) VALUES ('owner', "
        "'local:owner', 'admin', 'free', to_char(now() AT TIME ZONE 'utc', "
        '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')) ON CONFLICT DO NOTHING'
    )
    op.execute(
        f"""
        INSERT INTO user_jobs (user_id, job_id, score, score_reason, status, user_status_at,
            notion_page_id, notion_synced_at, created_at, updated_at)
        SELECT 'owner', id, score, score_reason, CASE WHEN status IN {MINE} THEN status END,
            user_status_at, notion_page_id, notion_synced_at, created_at, updated_at
        FROM job
        WHERE score IS NOT NULL OR score_reason IS NOT NULL OR status IN {MINE}
            OR user_status_at IS NOT NULL OR notion_page_id IS NOT NULL
        """  # noqa: S608 (MINE is a constant above)
    )
    op.drop_constraint("job_status_value", "job", type_="check")
    op.execute(
        f"""
        UPDATE job SET
            status_reason = CASE WHEN status IN {MINE} THEN NULL ELSE score_reason END,
            status = CASE WHEN status NOT IN {MINE} THEN status
                          WHEN data_json IS NULL THEN 'discovered' ELSE 'extracted' END
        """  # noqa: S608 (MINE is a constant above)
    )
    op.create_check_constraint("job_status_value", "job", f"status IN {CATALOG}")
    for column in ("score", "score_reason", "user_status_at", "notion_page_id", "notion_synced_at"):
        op.drop_column("job", column)


def downgrade() -> None:
    raise NotImplementedError("user_jobs can't be folded back into job without losing data")
