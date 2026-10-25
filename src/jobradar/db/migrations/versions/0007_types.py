"""Times become `timestamptz` and JSON becomes `jsonb` (SaaS PR 5e). Postgres then checks
them, sorts and compares times as times, and can index inside JSON. JobRadar still reads
both as text (`db/types.py`). Empty strings become NULL; `\\u0000`, which jsonb can't hold, is
dropped from JSON first.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMES = {
    "feed_board": ("last_checked", "last_ok", "skip_until"),
    "job": ("created_at", "updated_at"),
    "llm_cache": ("created_at",),
    "task": ("run_after", "started_at"),
    "watch_page": ("last_checked", "last_ok", "wait_until"),
    "fetched_page": ("fetched_at",),
    "match_result": ("created_at",),
    "raw_message": ("posted_at", "received_at"),
    "users": ("created_at", "last_seen_at", "deleted_at"),
    "alert": ("queued_at", "sent_at"),
    "artifact": ("created_at",),
    "preferences": ("updated_at",),
    "profiles": ("updated_at",),
    "status_change": ("changed_at",),
    "user_jobs": ("user_status_at", "notion_synced_at", "created_at", "updated_at"),
}
JSONS = {
    "feed_board": ("seen_json", "latest_json"),
    "job": ("data_json",),
    "task": ("payload",),
    "fetched_page": ("structured_json",),
    "match_result": ("answers_json",),
    "raw_message": ("urls_json",),
    "artifact": ("content_json",),
    "preferences": ("preferences_json",),
    "profiles": ("profile_json",),
}


def upgrade() -> None:
    for table, columns in TIMES.items():
        changes = ", ".join(
            f"ALTER COLUMN {c} TYPE timestamptz USING NULLIF({c}, '')::timestamptz" for c in columns
        )
        op.execute(f"ALTER TABLE {table} {changes}")
    for table, columns in JSONS.items():
        changes = ", ".join(
            f"ALTER COLUMN {c} TYPE jsonb USING NULLIF(replace({c}, '\\u0000', ''), '')::jsonb"
            for c in columns
        )
        op.execute(f"ALTER TABLE {table} {changes}")


def downgrade() -> None:
    for table, columns in {**TIMES, **JSONS}.items():
        changes = ", ".join(f"ALTER COLUMN {c} TYPE varchar USING {c}::text" for c in columns)
        op.execute(f"ALTER TABLE {table} {changes}")
