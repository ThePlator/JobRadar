"""All database queries (swap SQLite/Postgres here).

Every method opens its own short transaction. Task-queue state changes are single SQL
statements, so they stay atomic even if the process dies between calls.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, bindparam, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable
from sqlmodel import Session, SQLModel, col, create_engine, select

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db import models  # also registers every table on SQLModel.metadata
from jobradar.db.models import (
    CATALOG_STATUSES,
    OWNER,
    USER_STATUSES,
    Artifact,
    FeedBoard,
    FetchedPage,
    Job,
    JobStatus,
    Platform,
    RawMessage,
    Source,
    TaskStatus,
    User,
    WatchedPage,
)
from jobradar.db.textsearch import TextQuery

DEFAULT_DB_PATH = Path("data/jobradar.db")
OWNER_AUTH_UID = "local:owner"  # until the admin signs in to the SaaS for the first time


def connect(database_url: str | None, path: Path = DEFAULT_DB_PATH) -> Engine:
    """Postgres when `database_url` is set (the SaaS), else the SQLite file at `path`."""
    if not database_url:
        return make_engine(path)
    if database_url.startswith("postgres://"):  # what some providers print
        database_url = "postgresql://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    # pre_ping: Neon and other serverless hosts close idle connections; reconnect quietly.
    # prepare_threshold None: no server-side prepared statements, which a connection pooler
    # (Neon's "-pooler" address, PgBouncer) can't keep across transactions.
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"prepare_threshold": None},
    )
    _as_the_user(engine)
    return engine


# The role that can't bypass row-level security: one per database (migration 0006).
APP_ROLE = "current_database() || '_app'"


def _as_the_user(engine: Engine) -> None:
    """A transaction on a user's scoped repo (`Repo.scoped`) runs as the database's app role with
    `app.user_id` set, so Postgres itself only shows and accepts that user's rows of the
    per-user tables (SAASPLAN.md section 9). Both last until the transaction ends."""

    @event.listens_for(engine, "begin")
    def _begin(conn: Connection) -> None:
        user_id = conn.get_execution_options().get("jobradar_user")
        if user_id:
            conn.exec_driver_sql(
                f"SELECT set_config('role', {APP_ROLE}, true),"  # SET LOCAL ROLE
                " set_config('app.user_id', %(uid)s, true)",
                {"uid": str(user_id)},
            )


def make_engine(path: Path = DEFAULT_DB_PATH) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    """Bring the schema up to date: Alembic migrations on Postgres; on SQLite (tests and the
    self-hosted app) create_all plus the two small repairs below."""
    if engine.dialect.name == "postgresql":
        from jobradar.db.migrate import upgrade

        upgrade(engine)
    else:
        SQLModel.metadata.create_all(engine)
        _add_new_columns(engine)
        _refresh_source_platforms(engine)
    _add_plans(engine)
    add_owner(engine)
    if engine.dialect.name == "sqlite":
        _split_user_jobs(engine)
        _own_old_rows(engine)


def add_owner(engine: Engine) -> None:
    """The self-hosted app's one user, who owns every job's score and status until others
    sign in. In the SaaS the admin's first sign-in takes this account over."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, auth_uid, role, plan_id, created_at) "
                "VALUES (:id, :uid, 'admin', 'free', :now) ON CONFLICT DO NOTHING"
            ),
            {"id": OWNER, "uid": OWNER_AUTH_UID, "now": to_iso(utcnow())},
        )


# Setting keys that are one user's, not the app's: moved to user_settings (migration 0004).
USER_SETTING_PREFIXES = (
    "closing_reminder:", "company_replied:", "followed_up:", "followup_reminder:",
    "skill_gaps_week",
)  # fmt: skip


def _own_old_rows(engine: Engine) -> None:
    """A database from before user_id: `alert` was one row per job (keyed by job_id alone), and
    per-job notes lived in `setting`. Make both the owner's. Postgres: migration 0004."""
    with engine.begin() as c:
        keys = [row[1] for row in c.execute(text("PRAGMA table_info('alert')")) if row[5]]
        if keys == ["job_id"]:  # the old key: one alert per user and job needs its own index
            c.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS alert_user_job ON alert (user_id, job_id)")
            )
    move_user_settings(engine)


def move_user_settings(engine: Engine) -> None:
    """Per-job notes kept in `setting` (one user's, from before users) become the owner's."""
    with engine.begin() as c:
        for prefix in USER_SETTING_PREFIXES:
            like = {"p": prefix.replace("_", "\\_") + "%", "owner": OWNER}
            c.execute(
                text(
                    "INSERT INTO user_settings (user_id, key, value) SELECT :owner, key, value "
                    "FROM setting WHERE key LIKE :p ESCAPE '\\' ON CONFLICT DO NOTHING"
                ),
                like,
            )
            c.execute(text("DELETE FROM setting WHERE key LIKE :p ESCAPE '\\'"), like)


def _split_user_jobs(engine: Engine) -> None:
    """A database from before user_jobs (the self-hosted app's) keeps score and status on
    `job`: move them to the owner's user_jobs rows, once. Postgres does the same in
    migration 0003."""
    with engine.begin() as c:
        have = {row[1] for row in c.execute(text("PRAGMA table_info('job')"))}
        if "score" not in have or c.execute(text("SELECT 1 FROM user_jobs LIMIT 1")).first():
            return
        mine = ", ".join(repr(s.value) for s in USER_STATUSES)
        c.execute(
            text(
                f"""
                INSERT INTO user_jobs (user_id, job_id, score, score_reason, status,
                    user_status_at, notion_page_id, notion_synced_at, created_at, updated_at)
                SELECT :owner, id, score, score_reason,
                    CASE WHEN status IN ({mine}) THEN status END,
                    user_status_at, notion_page_id, notion_synced_at, created_at, updated_at
                FROM job
                WHERE score IS NOT NULL OR score_reason IS NOT NULL OR status IN ({mine})
                    OR user_status_at IS NOT NULL OR notion_page_id IS NOT NULL
                """  # noqa: S608 (statuses come from the enum)
            ),
            {"owner": OWNER},
        )
        c.execute(
            text(
                f"""
                UPDATE job SET
                    status_reason = CASE WHEN status IN ({mine}) THEN NULL ELSE score_reason END,
                    status = CASE WHEN status NOT IN ({mine}) THEN status
                                  WHEN data_json IS NULL THEN 'discovered' ELSE 'extracted' END
                """  # noqa: S608 (statuses come from the enum)
            )
        )


def _add_plans(engine: Engine) -> None:
    """The plans every new user can be on. Existing rows are left alone, so numbers changed
    in the database stay changed."""
    with engine.begin() as c:
        for plan in models.PLANS:
            c.execute(
                text(
                    "INSERT INTO plans (id, llm_calls_per_day, resume_builds_per_day, "
                    "emails_per_day, followed_sources_max, api_tokens_max) VALUES (:id, "
                    ":llm_calls_per_day, :resume_builds_per_day, :emails_per_day, "
                    ":followed_sources_max, :api_tokens_max) ON CONFLICT (id) DO NOTHING"
                ),
                plan.model_dump(),
            )


def _refresh_source_platforms(engine: Engine) -> None:
    """SQLite can't change a CHECK constraint in place, so when a new source platform appears
    (e.g. "board"), `source` is rebuilt with the new list. A backup of the database file is
    written next to it first."""
    with engine.connect() as c:
        sql = c.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'source'")
        ).scalar()
    if sql is None:
        return
    new = [p.value for p in Platform if f"'{p.value}'" not in sql]
    if not new:
        return
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        if dbapi is None:
            raise RuntimeError("no database connection to rebuild `source` with")
        if path := engine.url.database:
            # Named after what's new, so an earlier backup is never overwritten.
            backup = sqlite3.connect(f"{path}.before-{'-'.join(new)}-sources.bak")
            dbapi.backup(backup)
            backup.close()
        columns = ", ".join(c.name for c in models.Source.__table__.columns)  # type: ignore[attr-defined]
        create = str(CreateTable(models.Source.__table__).compile(engine))  # type: ignore[attr-defined]
        dbapi.execute("PRAGMA foreign_keys = OFF")
        dbapi.execute("PRAGMA legacy_alter_table = ON")  # keep raw_message pointing at "source"
        try:
            dbapi.execute("BEGIN")
            dbapi.execute("ALTER TABLE source RENAME TO source_old")
            dbapi.execute(create)
            dbapi.execute(f"INSERT INTO source ({columns}) SELECT {columns} FROM source_old")  # noqa: S608 (column names from the model)
            dbapi.execute("DROP TABLE source_old")
            problems = dbapi.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key problems after rebuilding source: {problems[:3]}")
            dbapi.execute("COMMIT")
        except Exception:
            dbapi.execute("ROLLBACK")
            raise
        finally:
            dbapi.execute("PRAGMA legacy_alter_table = OFF")
            dbapi.execute("PRAGMA foreign_keys = ON")
    finally:
        raw.close()


def _add_new_columns(engine: Engine) -> None:
    """create_all makes missing tables but not columns added to existing ones. Adds them;
    only nullable columns may be added to an existing table."""
    with engine.begin() as c:
        for table in SQLModel.metadata.sorted_tables:
            have = {row[1] for row in c.execute(text(f"PRAGMA table_info('{table.name}')"))}
            for column in table.columns:
                if column.name not in have:
                    kind = column.type.compile(engine.dialect)
                    extra = ""
                    if column.name == "user_id":  # rows from before users: the owner's
                        extra = f" NOT NULL DEFAULT '{OWNER}'"
                    elif not column.nullable:
                        raise RuntimeError(f"cannot add required column {table.name}.{column.name}")
                    c.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}{extra}')
                    )


# A job as one user sees it (models.Job): the posting with that user's score and status. Queries
# read it as `FROM {JOB_VIEW} j` and pass the user's id as :uid.
JOB_VIEW = """(SELECT j0.id, j0.canonical_url, j0.fingerprint, j0.company, j0.role, j0.deadline,
        j0.data_json, u.score,
        CASE WHEN u.status IS NOT NULL THEN u.score_reason
             ELSE coalesce(j0.status_reason, u.score_reason) END AS score_reason,
        j0.scam_risk, coalesce(u.status, j0.status) AS status, u.notion_page_id,
        u.notion_synced_at, j0.prompt_version, u.user_status_at, j0.created_at, j0.updated_at
    FROM job j0 LEFT JOIN user_jobs u ON u.job_id = j0.id AND u.user_id = :uid)"""


def _among(column: str, values: list[str]) -> tuple[str, dict[str, Any]]:
    """`column IN (...)` with one bound parameter per value; never true for no values."""
    if not values:
        return "1 = 0", {}
    names = {f"v{i}": v for i, v in enumerate(values)}
    return f"{column} IN ({', '.join(':' + n for n in names)})", names


@dataclass(frozen=True)
class Sighting:
    """One message that posted a job, with where it came from."""

    platform: str
    chat_id: str
    chat_title: str | None
    message_id: str
    text: str | None
    posted_at: str


@dataclass(frozen=True)
class TaskRecord:
    id: int
    type: str
    key: str
    payload: dict[str, Any]
    attempts: int  # including the current attempt


class Repo:
    """Every query. Per-user ones (scores, statuses) are for `user_id` unless told otherwise:
    `for_user` gives the same database for someone else."""

    def __init__(
        self, engine: Engine, clock: Clock = utcnow, user_id: str = OWNER,
        scoped: bool = False, root: Engine | None = None,
    ) -> None:  # fmt: skip
        self.engine = engine
        self._root = root or engine
        self._clock = clock
        self.user_id = user_id
        self.is_scoped = scoped
        self.pg = engine.dialect.name == "postgresql"

    def scoped(self, user_id: str) -> Repo:
        """A repo that can only reach this user's rows, for what a signed-in user asks: every
        query filters by them, and on Postgres row-level security makes sure of it."""
        root = self._root
        engine = root.execution_options(jobradar_user=user_id) if self.pg else root
        return Repo(engine, self._clock, user_id, scoped=True, root=root)

    def for_user(self, user_id: str) -> Repo:
        """The same database for someone else: scoped if this one is."""
        if user_id == self.user_id:
            return self
        if self.is_scoped:
            return self.scoped(user_id)
        return Repo(self._root, self._clock, user_id, root=self._root)

    def _uid(self, user_id: str | None) -> str:
        return user_id or self.user_id

    def _minutes(self, start: str, end: str) -> str:
        """SQL for the minutes between two ISO time columns."""
        if self.pg:
            span = f"CAST({end} AS timestamptz) - CAST({start} AS timestamptz)"
            return f"EXTRACT(EPOCH FROM ({span})) / 60"
        return f"(julianday({end}) - julianday({start})) * 1440"

    def _now(self) -> str:
        return to_iso(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def _jobs(
        self,
        where: str = "1 = 1",
        params: dict[str, Any] | None = None,
        order: str = "j.created_at, j.id",
        limit: int | None = None,
        offset: int = 0,
        user_id: str | None = None,
    ) -> list[Job]:
        """Jobs as this user sees them, filtered by `where` over JOB_VIEW's columns (`j.`)."""
        sql = f"SELECT * FROM {JOB_VIEW} j WHERE {where} ORDER BY {order}"  # noqa: S608
        if limit is not None:
            sql += " LIMIT :limit OFFSET :offset"
        with self._tx() as c:
            rows = c.execute(
                text(sql),
                {**(params or {}), "uid": self._uid(user_id), "limit": limit, "offset": offset},
            ).mappings()
            return [Job(**row) for row in rows]

    # ---- sources and messages ---------------------------------------------------------------

    def upsert_source(self, platform: Platform, chat_id: str, title: str | None = None) -> Source:
        with Session(self.engine) as s:
            src = s.exec(
                select(Source).where(Source.platform == platform.value, Source.chat_id == chat_id)
            ).first()
            if src is None:
                src = Source(platform=platform.value, chat_id=chat_id, title=title)
            elif title and src.title != title:
                src.title = title
            s.add(src)
            s.commit()
            s.refresh(src)
            return src

    def set_last_msg_id(self, source_id: int, last_msg_id: int) -> None:
        """Advance the catch-up point; never moves it backwards."""
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE source SET last_msg_id = :m "
                    "WHERE id = :id AND (last_msg_id IS NULL OR last_msg_id < :m)"
                ),
                {"id": source_id, "m": last_msg_id},
            )

    def save_raw_message(
        self,
        source_id: int,
        message_id: str,
        posted_at: str,
        text_: str | None = None,
        urls: list[str] | None = None,
        media_path: str | None = None,
    ) -> tuple[int, bool]:
        """Store a message before any processing. Returns (id, created); duplicates are no-ops."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "INSERT INTO raw_message "
                    "(source_id, message_id, text, urls_json, media_path, posted_at, received_at,"
                    " processed) "
                    "VALUES (:sid, :mid, :text, :urls, :media, :posted, :now, FALSE) "
                    "ON CONFLICT (source_id, message_id) DO NOTHING RETURNING id"
                ),
                {
                    "sid": source_id,
                    "mid": message_id,
                    "text": text_,
                    "urls": json.dumps(urls or []),
                    "media": media_path,
                    "posted": posted_at,
                    "now": self._now(),
                },
            ).first()
            if row is not None:
                return int(row[0]), True
            existing = c.execute(
                text("SELECT id FROM raw_message WHERE source_id = :sid AND message_id = :mid"),
                {"sid": source_id, "mid": message_id},
            ).one()
            return int(existing[0]), False

    def get_raw_message(self, raw_id: int) -> RawMessage | None:
        with Session(self.engine) as s:
            return s.get(RawMessage, raw_id)

    def mark_processed(self, raw_id: int) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE raw_message SET processed = TRUE WHERE id = :id"), {"id": raw_id}
            )

    # ---- jobs ---------------------------------------------------------------------------------

    def get_or_create_job(self, job_id: str, canonical_url: str | None) -> tuple[Job, bool]:
        """Returns (job, created). Safe to call repeatedly for the same id."""
        now = self._now()
        with self._tx() as c:
            created = (
                c.execute(
                    text(
                        "INSERT INTO job (id, canonical_url, status, created_at, updated_at) "
                        "VALUES (:id, :url, :status, :now, :now) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {"id": job_id, "url": canonical_url, "status": JobStatus.DISCOVERED.value,
                     "now": now},
                ).first()
                is not None
            )  # fmt: skip
        job = self.get_job(job_id)
        if job is None:  # canonical_url belonged to another id; cannot happen with sha1 ids
            raise RuntimeError(f"job {job_id} conflicts on canonical_url {canonical_url}")
        return job, created

    def get_job(self, job_id: str, user_id: str | None = None) -> Job | None:
        """The job as this user sees it (their score and status), or None."""
        found = self._jobs("j.id = :id", {"id": job_id}, user_id=user_id)
        return found[0] if found else None

    def add_job_source(self, job_id: str, raw_message_id: int) -> bool:
        """Record that a message posted this job. True if this pairing is new."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "INSERT INTO job_source (job_id, raw_message_id) VALUES (:job, :raw) "
                    "ON CONFLICT DO NOTHING RETURNING job_id"
                ),
                {"job": job_id, "raw": raw_message_id},
            ).first()
        if row is not None:
            self.touch_job(job_id)
        return row is not None

    def touch_job(self, job_id: str) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE job SET updated_at = :now WHERE id = :id"),
                {"id": job_id, "now": self._now()},
            )

    def job_sightings(self, job_id: str) -> list[Sighting]:
        """Every message that posted this job, oldest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.platform, s.chat_id, s.title, m.message_id, m.text, m.posted_at "
                    "FROM job_source js JOIN raw_message m ON m.id = js.raw_message_id "
                    "JOIN source s ON s.id = m.source_id WHERE js.job_id = :job "
                    "ORDER BY m.posted_at, m.id"
                ),
                {"job": job_id},
            ).all()
        return [Sighting(*r) for r in rows]

    def job_post_contexts(self, job_id: str, limit: int = 30) -> list[tuple[str | None, list[str]]]:
        """For each post carrying this job's link: (post text, ids of the other jobs in it)."""
        agg = "string_agg(other.job_id, ',')" if self.pg else "group_concat(other.job_id)"
        with self._tx() as c:
            rows = c.execute(
                text(
                    f"""
                    SELECT m.id, m.text, {agg} FROM job_source js
                    JOIN raw_message m ON m.id = js.raw_message_id
                    LEFT JOIN job_source other
                        ON other.raw_message_id = m.id AND other.job_id != js.job_id
                    WHERE js.job_id = :job
                    GROUP BY m.id ORDER BY m.posted_at, m.id LIMIT :limit
                    """  # noqa: S608 (agg is one of two fixed strings)
                ),
                {"job": job_id, "limit": limit},
            ).all()
        return [(r[1], r[2].split(",") if r[2] else []) for r in rows]

    def job_urls(self, status: JobStatus, user_id: str | None = None) -> list[tuple[str, str]]:
        jobs = self._jobs("j.status = :status AND j.canonical_url IS NOT NULL",
                          {"status": status.value}, user_id=user_id)  # fmt: skip
        return [(j.id, j.canonical_url) for j in jobs if j.canonical_url]

    def jobs_with_sightings(
        self, at_least: int, status: JobStatus, user_id: str | None = None
    ) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    f"SELECT j.id FROM {JOB_VIEW} j JOIN job_source js ON js.job_id = j.id "  # noqa: S608
                    "WHERE j.status = :status GROUP BY j.id HAVING count(*) >= :n"
                ),
                {"status": status.value, "n": at_least, "uid": self._uid(user_id)},
            ).all()
        return [str(r[0]) for r in rows]

    def set_job_status(
        self, job_id: str, status: JobStatus, reason: str | None = None, user_id: str | None = None
    ) -> None:
        """Where the pipeline is with this job (discovered, extracted, needs a look, not a
        job: for everyone), or what it decided for this user (new, hidden)."""
        now = self._now()
        with self._tx() as c:
            if JobStatus(status) in CATALOG_STATUSES:
                self._catalog_status(c, job_id, JobStatus(status), reason, now)
                return
            c.execute(
                text(
                    """
                    INSERT INTO user_jobs (user_id, job_id, score_reason, status, created_at,
                        updated_at)
                    SELECT CAST(:uid AS VARCHAR), id,
                        coalesce(CAST(:reason AS VARCHAR), status_reason),
                        CAST(:status AS VARCHAR), CAST(:now AS VARCHAR), CAST(:now AS VARCHAR)
                    FROM job WHERE id = :id
                    ON CONFLICT (user_id, job_id) DO UPDATE SET status = excluded.status,
                        score_reason = coalesce(CAST(:reason AS VARCHAR), user_jobs.score_reason),
                        updated_at = excluded.updated_at
                    """
                ),
                {"uid": self._uid(user_id), "id": job_id, "status": status.value, "reason": reason,
                 "now": now},
            )  # fmt: skip
            c.execute(text("UPDATE job SET updated_at = :now WHERE id = :id"),
                      {"id": job_id, "now": now})  # fmt: skip

    def _catalog_status(
        self, c: Connection, job_id: str, status: JobStatus, reason: str | None, now: str
    ) -> None:
        """Move the job itself back through the pipeline. Everyone who hasn't set its status
        themselves sees it there again until it is matched anew."""
        c.execute(
            text(
                "UPDATE job SET status = :status, updated_at = :now,"
                " status_reason = coalesce(CAST(:reason AS VARCHAR), status_reason) "
                "WHERE id = :id"
            ),
            {"id": job_id, "status": status.value, "reason": reason, "now": now},
        )
        c.execute(
            text(
                "UPDATE user_jobs SET status = NULL, updated_at = :now "
                "WHERE job_id = :id AND user_status_at IS NULL AND status IS NOT NULL"
            ),
            {"id": job_id, "now": now},
        )

    def latest_artifact(self, job_id: str, kind: str = "resume") -> Artifact | None:
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(
                    Artifact.user_id == self.user_id, Artifact.job_id == job_id,
                    Artifact.kind == kind,
                )
                .order_by(col(Artifact.version).desc())
            )  # fmt: skip
            return s.exec(query).first()

    def next_version(self, job_id: str, kind: str = "resume") -> int:
        latest = self.latest_artifact(job_id, kind)
        return latest.version + 1 if latest else 1

    def save_artifact(
        self, job_id: str, kind: str, version: int, local_path: str, content_json: str
    ) -> Artifact:
        """One version of this job's resume (or kit)."""
        artifact = Artifact(
            user_id=self.user_id, job_id=job_id, kind=kind, version=version, local_path=local_path,
            content_json=content_json, created_at=self._now(),
        )  # fmt: skip
        with Session(self.engine) as s:
            s.add(artifact)
            s.commit()
            s.refresh(artifact)
        return artifact

    def artifacts_since(self, since: str, kind: str = "resume") -> int:
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT COUNT(*) FROM artifact "
                    "WHERE user_id = :uid AND kind = :k AND created_at >= :since"
                ),
                {"uid": self.user_id, "k": kind, "since": since},
            ).first()
        return int(row[0]) if row else 0

    def jobs_with_prefix(self, prefix: str) -> list[str]:
        """Ids of jobs starting with this (at most a few)."""
        if not re.fullmatch(r"[0-9a-f]{4,40}", prefix):
            return []
        with self._tx() as c:
            rows = c.execute(
                text("SELECT id FROM job WHERE id LIKE :p LIMIT 5"), {"p": prefix + "%"}
            ).all()
        return [str(r[0]) for r in rows]

    def jobs_in_notion(self, user_id: str | None = None) -> list[str]:
        """Ids of this user's jobs that have a Notion page."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT job_id FROM user_jobs "
                    "WHERE user_id = :uid AND notion_page_id IS NOT NULL"
                ),
                {"uid": self._uid(user_id)},
            ).all()
        return [str(r[0]) for r in rows]

    def set_notion_page(self, job_id: str, page_id: str | None, user_id: str | None = None) -> None:
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO user_jobs (user_id, job_id, notion_page_id, notion_synced_at,
                        created_at, updated_at)
                    SELECT CAST(:uid AS VARCHAR), id, CAST(:page AS VARCHAR),
                        CASE WHEN CAST(:page AS VARCHAR) IS NULL THEN NULL
                             ELSE CAST(:now AS VARCHAR) END,
                        CAST(:now AS VARCHAR), CAST(:now AS VARCHAR)
                    FROM job WHERE id = :id
                    ON CONFLICT (user_id, job_id) DO UPDATE SET
                        notion_page_id = excluded.notion_page_id,
                        notion_synced_at = excluded.notion_synced_at
                    """
                ),
                {"uid": self._uid(user_id), "id": job_id, "page": page_id, "now": now},
            )

    # ---- task queue ---------------------------------------------------------------------------

    def enqueue(
        self,
        type_: str,
        key: str,
        payload: dict[str, Any] | None = None,
        run_after: str = "",
        priority: int = 0,
    ) -> None:
        """Insert a task, or merge into the existing (type, key) row:

        - pending: payload replaced, keeps the earlier run_after
        - running: payload replaced and the task runs once more after the current attempt
        - done / failed: reset to pending with a fresh attempt count
        """
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO task (type, key, payload, status, attempts, run_after, rerun,"
                    " priority) "
                    "VALUES (:type, :key, :payload, 'pending', 0, :run_after, FALSE, :priority) "
                    "ON CONFLICT (type, key) DO UPDATE SET "
                    " payload = excluded.payload,"
                    " priority = CASE"
                    "   WHEN coalesce(task.priority, 0) > coalesce(excluded.priority, 0)"
                    "   THEN coalesce(task.priority, 0) ELSE excluded.priority END,"
                    " run_after = CASE task.status"
                    "   WHEN 'pending' THEN CASE WHEN excluded.run_after < task.run_after"
                    "     THEN excluded.run_after ELSE task.run_after END"
                    "   WHEN 'running' THEN task.run_after"
                    "   ELSE excluded.run_after END,"
                    " attempts = CASE WHEN task.status IN ('done', 'failed') THEN 0"
                    "   ELSE task.attempts END,"
                    " last_error = CASE WHEN task.status IN ('done', 'failed') THEN NULL"
                    "   ELSE task.last_error END,"
                    " rerun = CASE WHEN task.status = 'running' THEN TRUE ELSE task.rerun END,"
                    " status = CASE WHEN task.status IN ('done', 'failed') THEN 'pending'"
                    "   ELSE task.status END"
                ),
                {
                    "type": type_,
                    "key": key,
                    "payload": json.dumps(payload or {}),
                    "run_after": run_after or now,
                    "priority": priority,
                },
            )

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        """Atomically take the next ready task (optionally of the given types): highest
        priority first, then the oldest."""
        type_filter = ""
        params: dict[str, Any] = {"now": self._now()}
        if types:
            names = [f":t{i}" for i in range(len(types))]
            type_filter = f" AND type IN ({', '.join(names)})"
            params |= {f"t{i}": t for i, t in enumerate(types)}
        # Postgres: several workers claim at once; each skips rows another is taking.
        lock = " FOR UPDATE SKIP LOCKED" if self.pg else ""
        with self._tx() as c:
            # type_filter only adds generated placeholder names; values stay bound parameters.
            sql = (
                "UPDATE task SET status = 'running', attempts = attempts + 1,"  # noqa: S608
                " started_at = :now WHERE id = (SELECT id FROM task"
                " WHERE status = 'pending' AND run_after <= :now"
                f"{type_filter} ORDER BY coalesce(priority, 0) DESC, run_after, id LIMIT 1{lock}) "
                "RETURNING id, type, key, payload, attempts"
            )
            row = c.execute(
                text(sql),
                params,
            ).first()
        if row is None:
            return None
        return TaskRecord(
            id=row.id,
            type=row.type,
            key=row.key,
            payload=json.loads(row.payload),
            attempts=row.attempts,
        )

    def complete(self, task_id: int) -> None:
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET"
                    " status = CASE WHEN rerun THEN 'pending' ELSE 'done' END,"
                    " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                    " run_after = CASE WHEN rerun THEN :now ELSE run_after END,"
                    " rerun = FALSE, started_at = NULL, last_error = NULL "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "now": now},
            )

    def fail(self, task_id: int, error: str, retry_at: str | None) -> None:
        """Record a failed attempt: back to pending at `retry_at`, or `failed` when None.

        A task re-enqueued while it was running always gets a fresh run instead.
        """
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET"
                    " status = CASE WHEN rerun THEN 'pending'"
                    "   WHEN CAST(:retry_at AS VARCHAR) IS NULL THEN 'failed' ELSE 'pending' END,"
                    " run_after = CASE WHEN rerun THEN :now"
                    "   ELSE coalesce(CAST(:retry_at AS VARCHAR), run_after) END,"
                    " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                    " rerun = FALSE, started_at = NULL, last_error = :error "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "error": error[:2000], "retry_at": retry_at, "now": self._now()},
            )

    def defer(self, task_id: int, until: str) -> None:
        """Put a running task back without counting the attempt (e.g. Telegram FloodWait)."""
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET status = 'pending', run_after = :until,"
                    " attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,"
                    " rerun = FALSE, started_at = NULL "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "until": until},
            )

    def reclaim_stuck(self, started_before: str) -> int:
        """Return tasks left `running` by a crash to `pending`. Returns how many."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "UPDATE task SET status = 'pending', run_after = :now, started_at = NULL,"
                    " rerun = FALSE WHERE status = 'running' AND started_at < :cutoff"
                ),
                {"now": self._now(), "cutoff": started_before},
            )
            return result.rowcount

    def requeue_failed(self) -> int:
        with self._tx() as c:
            result = c.execute(
                text(
                    "UPDATE task SET status = 'pending', attempts = 0, run_after = :now "
                    "WHERE status = 'failed'"
                ),
                {"now": self._now()},
            )
            return result.rowcount

    def has_ready_tasks(self, types: list[str]) -> bool:
        """True while any task of these types is running or due to run now."""
        names = [f":t{i}" for i in range(len(types))]
        params: dict[str, Any] = {"now": self._now()} | {f"t{i}": t for i, t in enumerate(types)}
        sql = (
            "SELECT 1 FROM task WHERE (status = 'running'"  # noqa: S608  (placeholders only)
            " OR (status = 'pending' AND run_after <= :now))"
            f" AND type IN ({', '.join(names)}) LIMIT 1"
        )
        with self._tx() as c:
            return c.execute(text(sql), params).first() is not None

    # ---- fetch and extraction ---------------------------------------------------------------

    def save_page(
        self,
        job_id: str,
        final_url: str | None,
        title: str | None,
        text_: str | None,
        note: str | None = None,
        structured: dict[str, Any] | None = None,
    ) -> None:
        with Session(self.engine) as s:
            page = s.get(FetchedPage, job_id) or FetchedPage(job_id=job_id, fetched_at="")
            page.final_url, page.title, page.text, page.note = final_url, title, text_, note
            page.structured_json = json.dumps(structured) if structured else None
            page.fetched_at = self._now()
            s.add(page)
            s.commit()

    def index_material(
        self, job_ids: list[str], posts: int = 3
    ) -> list[tuple[Job, str | None, list[str]]]:
        """For the search index, in three queries however many jobs: each job, its page text
        and the text of its first `posts` posts (oldest first, empty ones dropped)."""
        if not job_ids:
            return []
        ids: Any = bindparam("ids", expanding=True)
        where, params = _among("j.id", job_ids)
        jobs = self._jobs(where, params)  # only the posting's own fields are used
        with self._tx() as c:
            pages: dict[str, str | None] = dict(c.execute(
                text("SELECT job_id, text FROM fetched_page WHERE job_id IN :ids").bindparams(ids),
                {"ids": job_ids}).all())  # fmt: skip
            rows = c.execute(
                text(
                    "SELECT js.job_id, m.text FROM job_source js "
                    "JOIN raw_message m ON m.id = js.raw_message_id "
                    "WHERE js.job_id IN :ids ORDER BY js.job_id, m.posted_at, m.id"
                ).bindparams(ids),
                {"ids": job_ids},
            ).all()
        firsts: dict[str, list[str | None]] = {}
        for job_id, body in rows:
            seen = firsts.setdefault(job_id, [])
            if len(seen) < posts:
                seen.append(body)
        return [(j, pages.get(j.id), [t for t in firsts.get(j.id, []) if t]) for j in jobs]

    def get_page(self, job_id: str) -> FetchedPage | None:
        with Session(self.engine) as s:
            return s.get(FetchedPage, job_id)

    def save_extraction(
        self, job_id: str, data_json: str, company: str | None, role: str | None,
        deadline: str | None, prompt_version: str, status: JobStatus,
        fingerprint: str | None = None,
    ) -> None:  # fmt: skip
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET data_json = :data, company = :company, role = :role,
                        deadline = :deadline, prompt_version = :version,
                        fingerprint = :fingerprint, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "data": data_json, "company": company, "role": role,
                 "deadline": deadline, "version": prompt_version,
                 "fingerprint": fingerprint, "now": now},
            )  # fmt: skip
            catalog = status if JobStatus(status) in CATALOG_STATUSES else JobStatus.EXTRACTED
            self._catalog_status(c, job_id, catalog, None, now)
        if catalog != status:  # e.g. NEW: read, and matched for the owner too
            self.set_job_status(job_id, status)

    def jobs_without_fingerprint(self) -> list[tuple[str, str | None, str | None]]:
        """(id, company, role) of extracted jobs with no role key yet."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id, company, role FROM job WHERE fingerprint IS NULL "
                    "AND data_json IS NOT NULL AND status != 'discarded'"
                )
            ).all()
        return [(str(r[0]), r[1], r[2]) for r in rows]

    def set_fingerprints(self, keys: dict[str, str]) -> None:
        with self._tx() as c:
            for job_id, key in keys.items():
                c.execute(
                    text("UPDATE job SET fingerprint = :fp WHERE id = :id"),
                    {"fp": key, "id": job_id},
                )

    def same_role_main(
        self, job_id: str, fingerprint: str, since: str, user_id: str | None = None
    ) -> str | None:
        """The oldest open job with this company and role, first seen since `since` and
        before this one, that you haven't moved in Notion; None if there is none."""
        found = self._jobs(
            "j.fingerprint = :fp AND j.id != :id AND j.user_status_at IS NULL"
            " AND j.status IN ('extracted', 'new') AND j.created_at >= :since"
            " AND j.created_at < (SELECT created_at FROM job WHERE id = :id)",
            {"fp": fingerprint, "id": job_id, "since": since},
            order="j.created_at, j.id", limit=1, user_id=user_id,
        )  # fmt: skip
        return found[0].id if found else None

    def copy_sightings(self, from_job: str, to_job: str) -> int:
        """Add every message that posted `from_job` to `to_job` too. Returns how many."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "INSERT INTO job_source (job_id, raw_message_id) "
                    "SELECT :to, raw_message_id FROM job_source WHERE job_id = :from "
                    "ON CONFLICT DO NOTHING"
                ),
                {"to": to_job, "from": from_job},
            )
        if result.rowcount:
            self.touch_job(to_job)
        return int(result.rowcount or 0)

    def set_job_data(self, job_id: str, data_json: str) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE job SET data_json = :data, updated_at = :now WHERE id = :id"),
                {"id": job_id, "data": data_json, "now": self._now()},
            )

    # ---- matching -----------------------------------------------------------------------------

    def save_match(self, job_id: str, version: str, provider: str, answers_json: str) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO match_result (job_id, profile_version, provider, answers_json,
                        created_at)
                    VALUES (:job, :version, :provider, :answers, :now)
                    ON CONFLICT (job_id, profile_version) DO UPDATE SET
                        provider = excluded.provider, answers_json = excluded.answers_json,
                        created_at = excluded.created_at
                    """
                ),
                {"job": job_id, "version": version, "provider": provider,
                 "answers": answers_json, "now": self._now()},
            )  # fmt: skip

    def ever_matched(self, job_id: str) -> bool:
        """True if this job has answers for any profile version."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT 1 FROM match_result WHERE job_id = :job LIMIT 1"), {"job": job_id}
            ).first()
        return row is not None

    def set_match_score(
        self, job_id: str, score: int | None, reason: str, user_id: str | None = None
    ) -> bool:
        """Store this user's score; an extracted job becomes `new` (in their Inbox). True if
        anything changed."""
        now = self._now()
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    INSERT INTO user_jobs (user_id, job_id, score, score_reason, status,
                        created_at, updated_at)
                    SELECT CAST(:uid AS VARCHAR), id, CAST(:score AS INTEGER),
                        CAST(:reason AS VARCHAR),
                        CASE WHEN status = 'extracted' THEN 'new' END,
                        CAST(:now AS VARCHAR), CAST(:now AS VARCHAR)
                    FROM job WHERE id = :id
                    ON CONFLICT (user_id, job_id) DO UPDATE SET score = excluded.score,
                        score_reason = excluded.score_reason,
                        status = coalesce(user_jobs.status, excluded.status),
                        updated_at = excluded.updated_at
                    WHERE user_jobs.score IS DISTINCT FROM excluded.score
                        OR user_jobs.score_reason IS DISTINCT FROM excluded.score_reason
                        OR (user_jobs.status IS NULL AND excluded.status IS NOT NULL)
                    """
                ),
                {
                    "uid": self._uid(user_id),
                    "id": job_id,
                    "score": score,
                    "reason": reason,
                    "now": now,
                },
            )
            if result.rowcount:
                c.execute(text("UPDATE job SET updated_at = :now WHERE id = :id"),
                          {"id": job_id, "now": now})  # fmt: skip
        return bool(result.rowcount)

    def jobs_to_match(self, version: str, user_id: str | None = None) -> list[str]:
        """Open jobs without answers for this profile version."""
        jobs = self._jobs(
            "j.status IN ('extracted', 'new') AND NOT EXISTS (SELECT 1 FROM match_result m"
            " WHERE m.job_id = j.id AND m.profile_version = :version)",
            {"version": version}, order="j.created_at", user_id=user_id,
        )  # fmt: skip
        return [j.id for j in jobs]

    def match_answers(self, version: str, user_id: str | None = None) -> list[tuple[str, str]]:
        """(job_id, answers_json) for open jobs matched against this profile version."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    f"""
                    SELECT m.job_id, m.answers_json FROM match_result m
                    JOIN {JOB_VIEW} j ON j.id = m.job_id
                    WHERE m.profile_version = :version AND j.status IN ('extracted', 'new')
                    """  # noqa: S608
                ),
                {"version": version, "uid": self._uid(user_id)},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def set_scam_risk(self, job_id: str, risk: str) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE job SET scam_risk = :risk WHERE id = :id"),
                {"id": job_id, "risk": risk},
            )

    # ---- your statuses (Notion read path) ----------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(text("SELECT value FROM setting WHERE key = :k"), {"k": key}).first()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO setting (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                ),
                {"k": key, "v": value},
            )

    def get_user_setting(self, key: str) -> str | None:
        """One of this user's own bits of state (e.g. "followed_up:<job id>")."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT value FROM user_settings WHERE user_id = :uid AND key = :k"),
                {"uid": self.user_id, "k": key},
            ).first()
        return None if row is None else str(row[0])

    def set_user_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO user_settings (user_id, key, value) VALUES (:uid, :k, :v) "
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value"
                ),
                {"uid": self.user_id, "k": key, "v": value},
            )

    # ---- profile and preferences (one row each per user) -------------------------------------

    def _one_json(self, table: str, column: str) -> tuple[dict[str, Any], str | None] | None:
        with self._tx() as c:
            row = c.execute(
                text(f"SELECT {column}, source FROM {table} WHERE user_id = :uid"),  # noqa: S608
                {"uid": self.user_id},
            ).first()
        return None if row is None else (json.loads(row[0]), row[1])

    def _save_json(
        self, table: str, column: str, value: dict[str, Any], source: str | None
    ) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    f"INSERT INTO {table} (user_id, {column}, source, updated_at) "  # noqa: S608
                    "VALUES (:uid, :v, :src, :now) ON CONFLICT (user_id) DO UPDATE SET "
                    f"{column} = excluded.{column}, source = excluded.source, "
                    "updated_at = excluded.updated_at"
                ),
                {"uid": self.user_id, "v": json.dumps(value, ensure_ascii=False),
                 "src": source, "now": self._now()},
            )  # fmt: skip

    def get_profile(self) -> tuple[dict[str, Any], str | None] | None:
        """(this user's profile, where it was copied from), or None."""
        return self._one_json("profiles", "profile_json")

    def save_profile(self, profile: dict[str, Any], source: str | None = None) -> None:
        self._save_json("profiles", "profile_json", profile, source)

    def get_preferences(self) -> tuple[dict[str, Any], str | None] | None:
        return self._one_json("preferences", "preferences_json")

    def save_preferences(self, preferences: dict[str, Any], source: str | None = None) -> None:
        self._save_json("preferences", "preferences_json", preferences, source)

    # ---- users (the SaaS) -------------------------------------------------------------------

    def user_by_auth_uid(self, auth_uid: str) -> User | None:
        with Session(self.engine) as s:
            return s.exec(select(User).where(User.auth_uid == auth_uid)).first()

    def get_user(self, user_id: str) -> User | None:
        with Session(self.engine) as s:
            return s.get(User, user_id)

    def take_over_owner(self, auth_uid: str) -> bool:
        """The admin's first sign-in becomes the owner's account (which has every job's score
        and status). An account made for that sign-in before (with nothing of its own yet) is
        removed. False if the owner already belongs to someone."""
        with self._tx() as c:
            free = c.execute(
                text("SELECT 1 FROM users WHERE id = :owner AND auth_uid = :local"),
                {"owner": OWNER, "local": OWNER_AUTH_UID},
            ).first()
            if free is None:
                return False
            c.execute(
                text("DELETE FROM users WHERE auth_uid = :uid AND id != :owner"),
                {"uid": auth_uid, "owner": OWNER},
            )
            c.execute(
                text("UPDATE users SET auth_uid = :uid WHERE id = :owner"),
                {"uid": auth_uid, "owner": OWNER},
            )
        return True

    def add_user(self, user: User) -> User:
        with Session(self.engine) as s:
            s.add(user)
            s.commit()
            s.refresh(user)
            return user

    def saw_user(
        self, user_id: str, email: str | None, phone: str | None, name: str | None, role: str
    ) -> None:
        """Keep a user's details as their sign-in provider has them, and note when they were
        last here."""
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE users SET email = :email, phone = :phone, name = :name, "
                    "role = :role, last_seen_at = :now WHERE id = :id"
                ),
                {"id": user_id, "email": email, "phone": phone, "name": name, "role": role,
                 "now": self._now()},
            )  # fmt: skip

    def set_user_status(self, job_id: str, status: JobStatus, user_id: str | None = None) -> bool:
        """Record a Status you set (dashboard, email button, Notion). False if the job is
        unknown or unchanged."""
        now = self._now()
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    INSERT INTO user_jobs (user_id, job_id, score_reason, status, user_status_at,
                        created_at, updated_at)
                    SELECT CAST(:uid AS VARCHAR), id,
                        CASE WHEN CAST(:status AS VARCHAR) = 'hidden' THEN 'hidden by you'
                             ELSE status_reason END,
                        CAST(:status AS VARCHAR), CAST(:now AS VARCHAR), CAST(:now AS VARCHAR),
                        CAST(:now AS VARCHAR)
                    FROM job WHERE id = :id
                    ON CONFLICT (user_id, job_id) DO UPDATE SET status = excluded.status,
                        user_status_at = excluded.user_status_at,
                        updated_at = excluded.updated_at,
                        score_reason = CASE WHEN excluded.status = 'hidden' THEN 'hidden by you'
                                            ELSE user_jobs.score_reason END
                    WHERE user_jobs.status IS DISTINCT FROM excluded.status
                        OR user_jobs.user_status_at IS NULL
                    """
                ),
                {"uid": self._uid(user_id), "id": job_id, "status": status.value, "now": now},
            )
            if not result.rowcount:
                return False
            c.execute(text("UPDATE job SET updated_at = :now WHERE id = :id"),
                      {"id": job_id, "now": now})  # fmt: skip
            c.execute(
                text(
                    "INSERT INTO status_change (user_id, job_id, status, changed_at) "
                    "VALUES (:uid, :id, :status, :now)"
                ),
                {"uid": self._uid(user_id), "id": job_id, "status": status.value, "now": now},
            )
        return True

    def status_counts(self, user_id: str | None = None) -> dict[str, int]:
        """How many jobs are in each status right now, as this user sees them."""
        with self._tx() as c:
            rows = c.execute(
                text(f"SELECT j.status, count(*) FROM {JOB_VIEW} j GROUP BY j.status"),  # noqa: S608
                {"uid": self._uid(user_id)},
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def status_changes_since(self, since: str, until: str | None = None) -> dict[str, int]:
        """How many jobs you moved into each status since this time (and before `until`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, count(DISTINCT job_id) FROM status_change "
                    "WHERE user_id = :uid AND changed_at >= :since"
                    " AND (CAST(:until AS VARCHAR) IS NULL OR changed_at < :until) "
                    "GROUP BY status"
                ),
                {"uid": self.user_id, "since": since, "until": until},
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def scores_by_status(self, user_id: str | None = None) -> dict[str, list[int]]:
        """Match Scores of the jobs in each status (jobs without a score are left out)."""
        scores: dict[str, list[int]] = {}
        for job in self._jobs("j.score IS NOT NULL", user_id=user_id):
            scores.setdefault(job.status, []).append(int(job.score or 0))
        return scores

    # ---- company job boards -------------------------------------------------------------------

    def get_board(self, key: str) -> FeedBoard:
        with Session(self.engine) as s:
            return s.get(FeedBoard, key) or FeedBoard(key=key)

    def save_board(self, board: FeedBoard) -> None:
        with Session(self.engine) as s:
            s.merge(board)
            s.commit()

    # ---- watched pages ------------------------------------------------------------------------

    def get_watched_page(self, url: str, name: str) -> WatchedPage:
        with Session(self.engine) as s:
            return s.get(WatchedPage, url) or WatchedPage(url=url, name=name)

    def save_watched_page(self, page: WatchedPage) -> None:
        with Session(self.engine) as s:
            s.merge(page)
            s.commit()

    # ---- alerts -------------------------------------------------------------------------------

    def queue_alert(self, job_id: str) -> bool:
        """Record that this job deserves an alert. False if it ever had one."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "INSERT INTO alert (user_id, job_id, queued_at) VALUES (:uid, :job, :now) "
                    "ON CONFLICT (user_id, job_id) DO NOTHING"
                ),
                {"uid": self.user_id, "job": job_id, "now": self._now()},
            )
        return bool(result.rowcount)

    def pending_alerts(self) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT job_id FROM alert WHERE user_id = :uid AND outcome IS NULL "
                    "ORDER BY queued_at"
                ),
                {"uid": self.user_id},
            ).all()
        return [str(r[0]) for r in rows]

    def alerts_sent_since(self, since: str, until: str | None = None) -> int:
        """Alerts sent (or previewed in a dry run) since this time (and before `until`)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*) FROM alert WHERE user_id = :uid "
                    "AND outcome IN ('sent', 'dry_run') AND sent_at >= :since"
                    " AND (CAST(:until AS VARCHAR) IS NULL OR sent_at < :until)"
                ),
                {"uid": self.user_id, "since": since, "until": until},
            ).one()
        return int(row[0])

    def last_alert_sent_at(self) -> str | None:
        """When the latest alert email went out (or was previewed in a dry run)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT max(sent_at) FROM alert "
                    "WHERE user_id = :uid AND outcome IN ('sent', 'dry_run')"
                ),
                {"uid": self.user_id},
            ).one()
        return None if row[0] is None else str(row[0])

    def finish_alerts(self, job_ids: list[str], outcome: str) -> None:
        with self._tx() as c:
            for job_id in job_ids:
                c.execute(
                    text(
                        "UPDATE alert SET outcome = :outcome, sent_at = :now "
                        "WHERE user_id = :uid AND job_id = :job"
                    ),
                    {"uid": self.user_id, "job": job_id, "outcome": outcome, "now": self._now()},
                )

    # ---- digest -------------------------------------------------------------------------------

    def jobs_created_since(
        self, since: str, until: str | None = None, user_id: str | None = None
    ) -> list[Job]:
        return self._jobs(
            "j.created_at >= :since AND (CAST(:until AS VARCHAR) IS NULL OR j.created_at < :until)",
            {"since": since, "until": until}, user_id=user_id,
        )  # fmt: skip

    def jobs_you_moved_to(self, statuses: list[JobStatus], user_id: str | None = None) -> list[Job]:
        """Jobs whose Status you set to one of these, most recent change first."""
        where, params = _among("j.status", [st.value for st in statuses])
        return self._jobs(f"{where} AND j.user_status_at IS NOT NULL", params,
                          order="j.user_status_at DESC", user_id=user_id)  # fmt: skip

    def jobs_applied(self, user_id: str | None = None) -> list[Job]:
        """Jobs whose Status you set to Applied (and nothing since), oldest first."""
        return self._jobs("j.status = 'applied' AND j.user_status_at IS NOT NULL",
                          order="j.user_status_at", user_id=user_id)  # fmt: skip

    # ---- the dashboard's reads ------------------------------------------------------------

    def good_new_jobs(self, min_score: int, since: str, user_id: str | None = None) -> list[Job]:
        """New jobs scoring at least `min_score`, first seen since `since`, best first."""
        return self._jobs(
            "j.status = 'new' AND j.score >= :min AND j.created_at >= :since",
            {"min": min_score, "since": since},
            order="j.score DESC NULLS LAST, j.created_at DESC", user_id=user_id,
        )  # fmt: skip

    def jobs_in(self, statuses: list[JobStatus], user_id: str | None = None) -> list[Job]:
        """Jobs in these statuses, the most recently changed first."""
        where, params = _among("j.status", [st.value for st in statuses])
        order = "coalesce(j.user_status_at, j.updated_at) DESC, j.id"
        return self._jobs(where, params, order=order, user_id=user_id)

    def search_jobs(
        self,
        statuses: list[str] | None,
        words: str,
        limit: int,
        offset: int,
        order: str = "new",
        user_id: str | None = None,
    ) -> tuple[list[Job], int]:
        """A page of jobs in these statuses (None: any) whose company or role contains `words`,
        and how many match in all. `order`: "new" (newest first), "old" or "score"."""
        where, params = "1 = 1", dict[str, Any]()
        if statuses is not None:
            where, params = _among("j.status", statuses)
        if words := words.strip().lower():
            like = "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where += (" AND (lower(coalesce(j.company, '')) LIKE :like ESCAPE '\\'"
                      " OR lower(coalesce(j.role, '')) LIKE :like ESCAPE '\\')")  # fmt: skip
            params["like"] = like
        by = {
            "old": "j.created_at, j.id",
            "score": "j.score DESC NULLS LAST, j.created_at DESC",
        }.get(order, "j.created_at DESC, j.id")
        with self._tx() as c:
            total = c.execute(text(f"SELECT count(*) FROM {JOB_VIEW} j WHERE {where}"),  # noqa: S608
                              {**params, "uid": self._uid(user_id)}).one()[0]  # fmt: skip
        page = self._jobs(where, params, order=by, limit=limit, offset=offset, user_id=user_id)
        return page, int(total)

    # ---- full-text search (the dashboard's All jobs) ------------------------------------

    _FTS = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(job_id UNINDEXED, role, company, "
        "places, skills, body, tokenize = 'unicode61 remove_diacritics 2')"
    )

    def jobs_to_index(self, since: str | None) -> list[str]:
        """Jobs whose searchable text may have changed since `since` (all when None): the job
        itself, its page, or a new post about it."""
        with self._tx() as c:
            if not self.pg:
                c.execute(text(self._FTS))
            if since is None:
                rows = c.execute(text("SELECT id FROM job")).all()
            else:
                rows = c.execute(
                    text(
                        "SELECT id FROM job WHERE updated_at > :since "
                        "UNION SELECT job_id FROM fetched_page WHERE fetched_at > :since "
                        "UNION SELECT js.job_id FROM job_source js "
                        "JOIN raw_message m ON m.id = js.raw_message_id "
                        "WHERE m.received_at > :since"
                    ),
                    {"since": since},
                ).all()
        return [str(r[0]) for r in rows]

    def index_jobs(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        """(job id, role, company, places, skills, body) into the search index, replacing."""
        if not rows:
            return
        if self.pg:
            self._index_jobs_pg(rows)
            return
        with self._tx() as c:
            c.execute(text(self._FTS))
            for chunk in range(0, len(rows), 200):
                part = rows[chunk : chunk + 200]
                c.execute(
                    text("DELETE FROM job_fts WHERE job_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": [r[0] for r in part]},
                )
                c.execute(
                    text(
                        "INSERT INTO job_fts (job_id, role, company, places, skills, body) "
                        "VALUES (:id, :role, :company, :places, :skills, :body)"
                    ),
                    [
                        {
                            "id": r[0],
                            "role": r[1],
                            "company": r[2],
                            "places": r[3],
                            "skills": r[4],
                            "body": r[5],
                        }
                        for r in part
                    ],
                )

    def _index_jobs_pg(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        """Postgres: job_search keeps the text; its tsvector columns compute themselves."""
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO job_search (job_id, role, company, places, skills, body) "
                    "VALUES (:id, :role, :company, :places, :skills, :body) "
                    "ON CONFLICT (job_id) DO UPDATE SET role = excluded.role, "
                    "company = excluded.company, places = excluded.places, "
                    "skills = excluded.skills, body = excluded.body"
                ),
                [
                    # NUL can't be stored in Postgres text; pages occasionally contain it.
                    {
                        k: v.replace("\x00", "")
                        for k, v in zip(
                            ("id", "role", "company", "places", "skills", "body"), r, strict=True
                        )
                    }
                    for r in rows
                ],
            )

    def find_jobs(
        self,
        statuses: list[str] | None,
        match: TextQuery | None,
        min_score: int | None,
        since: str | None,
        source_id: int | None,
        order: str,
        limit: int,
        offset: int,
        user_id: str | None = None,
    ) -> tuple[list[tuple[Job, str | None]], int]:
        """A page of jobs for All jobs, with a text snippet around what `match` found, and how
        many match in all. `order`: new, old, score, or relevance."""
        where: list[str] = ["1 = 1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset, "uid": self._uid(user_id)}
        join = ""
        rank = "j.created_at DESC"
        snippet = "NULL"
        if match and self.pg:
            join = "JOIN job_search f ON f.job_id = j.id"
            if q := match.words_tsquery():
                where.append("f.doc @@ to_tsquery('simple', :q)")
                params["q"] = q
                rank = "ts_rank(f.doc, to_tsquery('simple', :q)) DESC"
                snippet = (
                    "ts_headline('simple', f.body, to_tsquery('simple', :q), "
                    "'StartSel=' || chr(2) || ', StopSel=' || chr(3) || "
                    "', MaxWords=18, MinWords=6, ShortWord=2, MaxFragments=1, "
                    "FragmentDelimiter=…')"
                )
            if pq := match.places_tsquery():
                where.append("f.places_doc @@ to_tsquery('simple', :pq)")
                params["pq"] = pq
        elif match:
            join = "JOIN job_fts f ON f.job_id = j.id"
            where.append("job_fts MATCH :match")
            params["match"] = match.fts5()
            rank = "bm25(job_fts, 0, 8, 6, 3, 4, 1)"
            snippet = "snippet(job_fts, -1, char(2), char(3), '…', 14)"
        if statuses is not None:
            marks = ", ".join(f":st{i}" for i in range(len(statuses)))
            where.append(f"j.status IN ({marks})" if statuses else "0 = 1")
            params |= {f"st{i}": st for i, st in enumerate(statuses)}
        if min_score is not None:
            where.append("j.score >= :min")
            params["min"] = min_score
        if since is not None:
            where.append("j.created_at >= :since")
            params["since"] = since
        if source_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM job_source js JOIN raw_message m ON m.id = js.raw_message_id"
                " WHERE js.job_id = j.id AND m.source_id = :src)"
            )
            params["src"] = source_id
        by = {
            "old": "j.created_at, j.id",
            "score": "j.score IS NULL, j.score DESC, j.created_at DESC",
            "relevance": rank,
        }.get(order, "j.created_at DESC, j.id")
        cond = " AND ".join(where)
        with self._tx() as c:
            if not self.pg:
                c.execute(text(self._FTS))
            total = c.execute(text(f"SELECT count(*) FROM {JOB_VIEW} j {join} WHERE {cond}"),  # noqa: S608
                              params).one()[0]  # fmt: skip
            rows = c.execute(
                text(f"SELECT j.id, {snippet} FROM {JOB_VIEW} j {join} WHERE {cond} "  # noqa: S608
                     f"ORDER BY {by} LIMIT :limit OFFSET :offset"),
                params,
            ).all()  # fmt: skip
        ids = [str(r[0]) for r in rows]
        in_ids, id_params = _among("j.id", ids)
        found = {j.id: j for j in self._jobs(in_ids, id_params, user_id=user_id)}
        return [(found[r[0]], r[1]) for r in rows if r[0] in found], int(total)

    def sources(self) -> list[tuple[int, str, str]]:
        """(id, platform, title) of every source that posted a job, by title."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.id, s.platform, coalesce(s.title, s.chat_id) FROM source s "
                    "WHERE EXISTS (SELECT 1 FROM raw_message m JOIN job_source js "
                    "ON js.raw_message_id = m.id WHERE m.source_id = s.id) ORDER BY 3"
                )
            ).all()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def last_activity(self) -> str | None:
        """When JobRadar last read anything: a message received or a board checked."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT max(t) FROM (SELECT max(received_at) AS t FROM raw_message "
                    "UNION ALL SELECT max(last_checked) FROM feed_board) AS seen"
                )
            ).one()
        return str(row[0]) if row[0] else None

    def latest_match(self, job_id: str) -> str | None:
        """The newest stored match answers (JSON) for this job, or None."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT answers_json FROM match_result WHERE job_id = :job "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"job": job_id},
            ).first()
        return str(row[0]) if row else None

    def good_job_sources(
        self, min_score: int, since: str, user_id: str | None = None
    ) -> list[tuple[str, str, int]]:
        """(platform, chat or board title, jobs) for jobs scoring `min_score`+ first seen since
        `since`, each job counted once per source."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.platform, coalesce(s.title, s.chat_id), count(DISTINCT j.id) "  # noqa: S608
                    f"FROM {JOB_VIEW} j JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message m ON m.id = js.raw_message_id "
                    "JOIN source s ON s.id = m.source_id "
                    "WHERE j.created_at >= :since AND j.score >= :min "
                    "GROUP BY s.platform, s.id ORDER BY 3 DESC"
                ),
                {"since": since, "min": min_score, "uid": self._uid(user_id)},
            ).all()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    def search_good_count(self, min_score: int, user_id: str | None = None) -> int:
        """Jobs that ever scored `min_score` or more (not counting what wasn't a job)."""
        with self._tx() as c:
            row = c.execute(
                text(f"SELECT count(*) FROM {JOB_VIEW} j "  # noqa: S608
                     "WHERE j.score >= :min AND j.status != 'discarded'"),
                {"min": min_score, "uid": self._uid(user_id)},
            ).one()  # fmt: skip
        return int(row[0])

    def jobs_ever_in(
        self, statuses: list[JobStatus], undo_minutes: int = 0, user_id: str | None = None
    ) -> int:
        """Jobs that are, or once were, in one of these statuses (by your status history).
        A status you changed again within `undo_minutes` is taken as a misclick."""
        gap = self._minutes("changed_at", "next_at")
        query = text(
            f"""
            WITH h AS (
                SELECT job_id, status, changed_at,
                       LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at, id)
                           AS next_at
                FROM status_change WHERE user_id = :uid
            )
            SELECT count(*) FROM (
                SELECT j.id FROM {JOB_VIEW} j WHERE j.status IN :sts
                UNION SELECT job_id FROM h WHERE status IN :sts AND (
                    next_at IS NULL
                    OR {gap} >= :undo)
            ) AS ever
            """  # noqa: S608 (gap is built from column names)
        ).bindparams(bindparam("sts", expanding=True))
        with self._tx() as c:
            row = c.execute(
                query,
                {
                    "sts": [st.value for st in statuses],
                    "undo": undo_minutes,
                    "uid": self._uid(user_id),
                },
            ).one()
        return int(row[0])

    def applications_by_source(
        self, undo_minutes: int = 10, user_id: str | None = None
    ) -> list[tuple[str, str, str, str, bool, bool]]:
        """(platform, source name, job id, its status now, reached the next round, found there
        first) for every job you applied to, once per source that carried it, newest find first.

        Applied means Applied or any later status in your history; the next round means Interview
        or Offer. A status changed again within `undo_minutes` is taken as a misclick and ignored,
        so Interview undone 8 seconds later isn't an interview."""
        gap = self._minutes("changed_at", "next_at")
        query = text(
            f"""
            WITH h AS (
                SELECT job_id, status, changed_at,
                       LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at, id)
                           AS next_at
                FROM status_change WHERE user_id = :uid
            ),
            kept AS (
                SELECT job_id, status FROM h
                WHERE next_at IS NULL
                   OR {gap} >= :undo
            ),
            outcome AS (
                SELECT job_id,
                       MAX(CASE WHEN status IN ('interview', 'offer') THEN 1 ELSE 0 END)
                           AS next_round
                FROM kept WHERE status IN ('applied', 'interview', 'offer', 'rejected')
                GROUP BY job_id
            ),
            per_source AS (  -- one row per job and source: a repost can't count twice
                SELECT m.source_id, o.job_id, o.next_round, MIN(m.received_at) AS seen
                FROM outcome o
                JOIN job_source js ON js.job_id = o.job_id
                JOIN raw_message m ON m.id = js.raw_message_id
                GROUP BY m.source_id, o.job_id, o.next_round
            ),
            firsts AS (SELECT job_id, MIN(seen) AS seen FROM per_source GROUP BY job_id)
            SELECT s.platform, coalesce(s.title, s.chat_id), p.job_id, j.status, p.next_round,
                   p.seen = f.seen
            FROM per_source p
            JOIN source s ON s.id = p.source_id
            JOIN {JOB_VIEW} j ON j.id = p.job_id
            JOIN firsts f ON f.job_id = p.job_id
            ORDER BY p.seen DESC
            """  # noqa: S608 (gap is built from column names)
        )
        with self._tx() as c:
            rows = c.execute(query, {"undo": undo_minutes, "uid": self._uid(user_id)}).all()
        return [(str(r[0]), str(r[1]), str(r[2]), str(r[3]), bool(r[4]), bool(r[5])) for r in rows]

    def get_artifact(self, artifact_id: int) -> Artifact | None:
        """This user's resume or kit by id; None for anyone else's."""
        with Session(self.engine) as s:
            found = s.get(Artifact, artifact_id)
        return found if found is not None and found.user_id == self.user_id else None

    def task_waiting(self, task_type: str, key: str) -> bool:
        """A task of this type and key is queued or running."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT 1 FROM task WHERE type = :type AND key = :key "
                    "AND status IN ('pending', 'running') LIMIT 1"
                ),
                {"type": task_type, "key": key},
            ).first()
        return row is not None

    def status_history(self, job_id: str) -> list[tuple[str, str]]:
        """(status, when) for every status you set on this job, oldest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, changed_at FROM status_change "
                    "WHERE user_id = :uid AND job_id = :job ORDER BY changed_at, id"
                ),
                {"uid": self.user_id, "job": job_id},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def artifacts_of(self, job_id: str) -> list[Artifact]:
        """Every resume and cover letter built for this job, newest first."""
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(Artifact.user_id == self.user_id, Artifact.job_id == job_id)
                .order_by(col(Artifact.created_at).desc(), col(Artifact.id).desc())
            )
            return list(s.exec(query))

    def jobs_closing(self, first_day: str, last_day: str, user_id: str | None = None) -> list[Job]:
        """Jobs you may still apply to whose deadline falls in these days (inclusive)."""
        return self._jobs(
            "j.status IN ('new', 'shortlisted', 'resume_ready')"
            " AND j.deadline >= :first AND j.deadline <= :last",
            {"first": first_day, "last": last_day},
            order="j.deadline, j.score DESC NULLS LAST", user_id=user_id,
        )  # fmt: skip

    def jobs_read_by_llm(self, user_id: str | None = None) -> list[Job]:
        """Jobs with extraction data, for comparing extraction methods."""
        return self._jobs("j.data_json IS NOT NULL AND j.status IN ('extracted', 'discarded')",
                          user_id=user_id)  # fmt: skip

    def jobs_missing_extraction(self, user_id: str | None = None) -> list[str]:
        """Jobs that reached Notion before extraction existed (for `jobradar extract`)."""
        jobs = self._jobs(
            "j.status = 'discovered' AND j.data_json IS NULL AND j.canonical_url IS NOT NULL",
            order="j.created_at", user_id=user_id,
        )  # fmt: skip
        return [j.id for j in jobs]

    def jobs_from_source(self, title: str, user_id: str | None = None) -> list[str]:
        """Jobs first or also seen in the source shown as `title` ("Workday · Accenture"),
        that you haven't moved in Notion (for `jobradar extract --again`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    f"SELECT DISTINCT j.id, j.created_at FROM {JOB_VIEW} j "  # noqa: S608
                    "JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message r ON r.id = js.raw_message_id "
                    "JOIN source s ON s.id = r.source_id "
                    "WHERE lower(s.title) = lower(:title) AND j.user_status_at IS NULL "
                    "AND j.status != 'discarded' AND j.canonical_url IS NOT NULL "
                    "ORDER BY j.created_at"
                ),
                {"title": title, "uid": self._uid(user_id)},
            ).all()
        return [str(r[0]) for r in rows]

    # ---- LLM cache and spend ------------------------------------------------------------------

    def llm_cache_get(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(
                text("SELECT output FROM llm_cache WHERE key = :key"), {"key": key}
            ).first()
        return None if row is None else str(row[0])

    def llm_cache_put(self, key: str, output: str, cost_inr: float) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO llm_cache (key, output, cost_inr, created_at) "
                    "VALUES (:key, :out, :cost, :now) ON CONFLICT (key) DO UPDATE SET "
                    "output = excluded.output, cost_inr = llm_cache.cost_inr + excluded.cost_inr, "
                    "created_at = excluded.created_at"
                ),
                {"key": key, "out": output, "cost": cost_inr, "now": self._now()},
            )

    def llm_spend_since(self, since: str) -> float:
        with self._tx() as c:
            row = c.execute(
                text("SELECT coalesce(sum(cost_inr), 0) FROM llm_cache WHERE created_at >= :s"),
                {"s": since},
            ).one()
        return float(row[0])

    # ---- reporting ----------------------------------------------------------------------------

    def message_counts(self, since: str) -> tuple[int, int]:
        """(messages received since, of which produced no job link)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*), sum(CASE WHEN processed AND NOT EXISTS ("
                    " SELECT 1 FROM job_source js WHERE js.raw_message_id = m.id)"
                    " THEN 1 ELSE 0 END) "
                    "FROM raw_message m WHERE m.received_at >= :since"
                ),
                {"since": since},
            ).one()
        return int(row[0]), int(row[1] or 0)

    def per_chat(self, since: str) -> list[tuple[str, int, int]]:
        """(chat title or id, posts received, distinct jobs they linked to), busiest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT coalesce(s.title, s.chat_id), count(DISTINCT m.id),"
                    " count(DISTINCT js.job_id) "
                    "FROM source s JOIN raw_message m ON m.source_id = s.id "
                    "LEFT JOIN job_source js ON js.raw_message_id = m.id "
                    "WHERE m.received_at >= :since GROUP BY s.id ORDER BY 2 DESC"
                ),
                {"since": since},
            ).all()
        return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]

    def jobs_since(
        self, since: str, user_id: str | None = None
    ) -> list[tuple[str, str | None, str, int, int | None, str | None, str | None]]:
        """(id, URL, status, sightings, first post id, first post text, reason) for jobs
        created since."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id, j.canonical_url, j.status, count(js.raw_message_id),"  # noqa: S608
                    " min(js.raw_message_id),"
                    " (SELECT m.text FROM job_source x"
                    "  JOIN raw_message m ON m.id = x.raw_message_id"
                    "  WHERE x.job_id = j.id ORDER BY m.posted_at, m.id LIMIT 1), "
                    " j.score_reason "
                    f"FROM {JOB_VIEW} j LEFT JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.created_at >= :since "
                    "GROUP BY j.id, j.canonical_url, j.status, j.score_reason, j.created_at "
                    "ORDER BY j.created_at"
                ),
                {"since": since, "uid": self._uid(user_id)},
            ).all()
        return [(str(r[0]), r[1], str(r[2]), int(r[3]), r[4], r[5], r[6]) for r in rows]

    def recent_failures(self, limit: int = 5) -> list[tuple[str, str, str]]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT type, key, coalesce(last_error, '') FROM task WHERE status = 'failed' "
                    "ORDER BY run_after DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    def task_counts(self) -> dict[TaskStatus, int]:
        with self._tx() as c:
            rows = c.execute(text("SELECT status, count(*) FROM task GROUP BY status")).all()
        counts = dict.fromkeys(TaskStatus, 0)
        counts.update({TaskStatus(r[0]): int(r[1]) for r in rows})
        return counts
