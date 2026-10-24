"""Today's schema, plus the full-text search table (Postgres's version of SQLite's FTS5).

Revision ID: 0001
Revises:
Create Date: 2026-10-24 23:17:13.274051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feed_board",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("seen_json", sa.String(), nullable=True),
        sa.Column("jobs_on_board", sa.Integer(), nullable=False),
        sa.Column("new_total", sa.Integer(), nullable=False),
        sa.Column("last_checked", sa.String(), nullable=True),
        sa.Column("last_ok", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("misses", sa.Integer(), nullable=False),
        sa.Column("skip_until", sa.String(), nullable=True),
        sa.Column("latest_json", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canonical_url", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("company", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("deadline", sa.String(), nullable=True),
        sa.Column("data_json", sa.String(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_reason", sa.String(), nullable=True),
        sa.Column("scam_risk", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("notion_page_id", sa.String(), nullable=True),
        sa.Column("notion_synced_at", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("user_status_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("scam_risk IN ('low', 'medium', 'high')", name="job_scam_risk"),
        sa.CheckConstraint(
            "status IN ('discovered', 'extracted', 'needs_review', 'discarded', 'new', 'hidden', 'shortlisted', 'resume_ready', 'applied', 'interview', 'offer', 'rejected', 'skipped')",
            name="job_status_value",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_url"),
    )
    op.create_index("job_fingerprint", "job", ["fingerprint"], unique=False)
    op.create_index("job_status", "job", ["status"], unique=False)
    op.create_table(
        "llm_cache",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("output", sa.String(), nullable=False),
        sa.Column("cost_inr", sa.Float(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_msg_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "platform IN ('telegram', 'email', 'whatsapp', 'manual', 'board', 'page')",
            name="source_platform",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "chat_id"),
    )
    op.create_table(
        "task",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("rerun", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')", name="task_status_value"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type", "key"),
    )
    op.create_index("task_ready", "task", ["status", "run_after"], unique=False)
    op.create_table(
        "watch_page",
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("last_checked", sa.String(), nullable=True),
        sa.Column("last_ok", sa.String(), nullable=True),
        sa.Column("last_changed", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("refusals", sa.Integer(), nullable=False),
        sa.Column("wait_until", sa.String(), nullable=True),
        sa.Column("posts_total", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("url"),
    )
    op.create_table(
        "alert",
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("queued_at", sa.String(), nullable=False),
        sa.Column("sent_at", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_table(
        "artifact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("local_path", sa.String(), nullable=True),
        sa.Column("drive_url", sa.String(), nullable=True),
        sa.Column("content_json", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint("kind IN ('resume', 'kit')", name="artifact_kind"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_artifact_job_id"), "artifact", ["job_id"], unique=False)
    op.create_table(
        "fetched_page",
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("final_url", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("structured_json", sa.String(), nullable=True),
        sa.Column("fetched_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_table(
        "match_result",
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("profile_version", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("answers_json", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("job_id", "profile_version"),
    )
    op.create_table(
        "raw_message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("urls_json", sa.String(), nullable=False),
        sa.Column("media_path", sa.String(), nullable=True),
        sa.Column("posted_at", sa.String(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "message_id"),
    )
    op.create_table(
        "status_change",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("changed_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_status_change_job_id"), "status_change", ["job_id"], unique=False)
    op.create_table(
        "job_source",
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("raw_message_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.ForeignKeyConstraint(
            ["raw_message_id"],
            ["raw_message.id"],
        ),
        sa.PrimaryKeyConstraint("job_id", "raw_message_id"),
    )

    # Search: role and company weigh most (A), then skills (B), places (C), then the page and
    # posts (D). 'simple' keeps words as written (no English stemming: tech names like "Go"
    # and "Spring" must stay exact), lower-cased.
    op.execute(
        """
        CREATE TABLE job_search (
            job_id text PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
            role text NOT NULL DEFAULT '',
            company text NOT NULL DEFAULT '',
            places text NOT NULL DEFAULT '',
            skills text NOT NULL DEFAULT '',
            body text NOT NULL DEFAULT '',
            doc tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', role), 'A')
                || setweight(to_tsvector('simple', company), 'A')
                || setweight(to_tsvector('simple', skills), 'B')
                || setweight(to_tsvector('simple', places), 'C')
                || setweight(to_tsvector('simple', body), 'D')
            ) STORED,
            places_doc tsvector GENERATED ALWAYS AS (to_tsvector('simple', places)) STORED
        )
        """
    )
    op.execute("CREATE INDEX job_search_doc ON job_search USING gin (doc)")
    op.execute("CREATE INDEX job_search_places ON job_search USING gin (places_doc)")


def downgrade() -> None:
    op.execute("DROP TABLE job_search")
    op.drop_table("job_source")
    op.drop_index(op.f("ix_status_change_job_id"), table_name="status_change")
    op.drop_table("status_change")
    op.drop_table("raw_message")
    op.drop_table("match_result")
    op.drop_table("fetched_page")
    op.drop_index(op.f("ix_artifact_job_id"), table_name="artifact")
    op.drop_table("artifact")
    op.drop_table("alert")
    op.drop_table("watch_page")
    op.drop_index("task_ready", table_name="task")
    op.drop_table("task")
    op.drop_table("source")
    op.drop_table("setting")
    op.drop_table("llm_cache")
    op.drop_index("job_status", table_name="job")
    op.drop_index("job_fingerprint", table_name="job")
    op.drop_table("job")
    op.drop_table("feed_board")
