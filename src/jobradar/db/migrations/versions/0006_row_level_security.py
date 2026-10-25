"""Row-level security on every per-user table (SaaS PR 5d). A signed-in user's requests run
as `<database>_app`, which only sees rows whose user_id is the transaction's `app.user_id`.
Background work runs as the login role, which owns the tables and so isn't limited.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PER_USER = ("user_jobs", "status_change", "alert", "artifact", "user_settings", "profiles",
            "preferences")  # fmt: skip
MINE = "user_id = current_setting('app.user_id', true)"  # unset: no rows at all
# One role per database (`<database>_app`): role names are shared by a whole server, and a
# staging and a live database may share one.
ROLE = "current_database() || '_app'"


def _each_role(sql: str) -> str:
    """Run `sql` with {role} as the quoted role name."""
    return f"DO $$ BEGIN EXECUTE format({sql!r}, {ROLE}); END $$"


def upgrade() -> None:
    exists = f"SELECT 1 FROM pg_roles WHERE rolname = {ROLE}"  # noqa: S608 (constants)
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS ({exists}) THEN "
        f"EXECUTE format('CREATE ROLE %I NOLOGIN', {ROLE}); END IF; END $$"
    )
    for grant in (
        "GRANT %I TO CURRENT_USER",  # so the app can switch to it
        "GRANT USAGE ON SCHEMA public TO %I",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I",
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I",
        "REVOKE ALL ON users FROM %I",  # accounts: sign-in and admin code only
    ):
        op.execute(_each_role(grant))
    for table in PER_USER:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {table} USING ({MINE}) WITH CHECK ({MINE})")


def downgrade() -> None:
    for table in PER_USER:
        op.execute(f"DROP POLICY own_rows ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(_each_role("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I"))
