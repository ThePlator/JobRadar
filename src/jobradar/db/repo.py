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

from sqlalchemy import Engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable
from sqlmodel import Session, SQLModel, col, create_engine, select

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db import models  # also registers every table on SQLModel.metadata
from jobradar.db.models import (
    Artifact,
    FeedBoard,
    FetchedPage,
    Job,
    JobStatus,
    Platform,
    RawMessage,
    Source,
    TaskStatus,
    WatchedPage,
)

DEFAULT_DB_PATH = Path("data/jobradar.db")


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
    SQLModel.metadata.create_all(engine)
    _add_new_columns(engine)
    _refresh_source_platforms(engine)


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
                    if not column.nullable:
                        raise RuntimeError(f"cannot add required column {table.name}.{column.name}")
                    kind = column.type.compile(engine.dialect)
                    c.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}'))


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
    def __init__(self, engine: Engine, clock: Clock = utcnow) -> None:
        self.engine = engine
        self._clock = clock

    def _now(self) -> str:
        return to_iso(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

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
                    "VALUES (:sid, :mid, :text, :urls, :media, :posted, :now, 0) "
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
            c.execute(text("UPDATE raw_message SET processed = 1 WHERE id = :id"), {"id": raw_id})

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

    def get_job(self, job_id: str) -> Job | None:
        with Session(self.engine) as s:
            return s.get(Job, job_id)

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
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT m.id, m.text, group_concat(other.job_id) FROM job_source js
                    JOIN raw_message m ON m.id = js.raw_message_id
                    LEFT JOIN job_source other
                        ON other.raw_message_id = m.id AND other.job_id != js.job_id
                    WHERE js.job_id = :job
                    GROUP BY m.id ORDER BY m.posted_at, m.id LIMIT :limit
                    """
                ),
                {"job": job_id, "limit": limit},
            ).all()
        return [(r[1], r[2].split(",") if r[2] else []) for r in rows]

    def job_urls(self, status: JobStatus) -> list[tuple[str, str]]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id, canonical_url FROM job "
                    "WHERE status = :status AND canonical_url IS NOT NULL"
                ),
                {"status": status.value},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def jobs_with_sightings(self, at_least: int, status: JobStatus) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id FROM job j JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.status = :status GROUP BY j.id HAVING count(*) >= :n"
                ),
                {"status": status.value, "n": at_least},
            ).all()
        return [str(r[0]) for r in rows]

    def set_job_status(self, job_id: str, status: JobStatus, reason: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET status = :status,
                        score_reason = coalesce(:reason, score_reason), updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "status": status.value, "reason": reason, "now": self._now()},
            )

    def latest_artifact(self, job_id: str, kind: str = "resume") -> Artifact | None:
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(Artifact.job_id == job_id, Artifact.kind == kind)
                .order_by(col(Artifact.version).desc())
            )
            return s.exec(query).first()

    def next_version(self, job_id: str, kind: str = "resume") -> int:
        latest = self.latest_artifact(job_id, kind)
        return latest.version + 1 if latest else 1

    def save_artifact(
        self, job_id: str, kind: str, version: int, local_path: str, content_json: str
    ) -> Artifact:
        """One version of this job's resume (or kit)."""
        artifact = Artifact(
            job_id=job_id, kind=kind, version=version, local_path=local_path,
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
                text("SELECT COUNT(*) FROM artifact WHERE kind = :k AND created_at >= :since"),
                {"k": kind, "since": since},
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

    def jobs_in_notion(self) -> list[str]:
        """Ids of jobs that have a Notion page."""
        with self._tx() as c:
            rows = c.execute(text("SELECT id FROM job WHERE notion_page_id IS NOT NULL")).all()
        return [str(r[0]) for r in rows]

    def set_notion_page(self, job_id: str, page_id: str | None) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE job SET notion_page_id = :page,"
                    " notion_synced_at = CASE WHEN :page IS NULL THEN NULL ELSE :now END "
                    "WHERE id = :id"
                ),
                {"id": job_id, "page": page_id, "now": self._now()},
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
                    "VALUES (:type, :key, :payload, 'pending', 0, :run_after, 0, :priority) "
                    "ON CONFLICT (type, key) DO UPDATE SET "
                    " payload = excluded.payload,"
                    " priority = max(coalesce(priority, 0), excluded.priority),"
                    " run_after = CASE status"
                    "   WHEN 'pending' THEN min(run_after, excluded.run_after)"
                    "   WHEN 'running' THEN run_after"
                    "   ELSE excluded.run_after END,"
                    " attempts = CASE WHEN status IN ('done', 'failed') THEN 0 ELSE attempts END,"
                    " last_error = CASE WHEN status IN ('done', 'failed') THEN NULL"
                    "   ELSE last_error END,"
                    " rerun = CASE WHEN status = 'running' THEN 1 ELSE rerun END,"
                    " status = CASE WHEN status IN ('done', 'failed') THEN 'pending'"
                    "   ELSE status END"
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
        with self._tx() as c:
            # type_filter only adds generated placeholder names; values stay bound parameters.
            sql = (
                "UPDATE task SET status = 'running', attempts = attempts + 1,"  # noqa: S608
                " started_at = :now WHERE id = (SELECT id FROM task"
                " WHERE status = 'pending' AND run_after <= :now"
                f"{type_filter} ORDER BY coalesce(priority, 0) DESC, run_after, id LIMIT 1) "
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
                    " rerun = 0, started_at = NULL, last_error = NULL "
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
                    "   WHEN :retry_at IS NULL THEN 'failed' ELSE 'pending' END,"
                    " run_after = CASE WHEN rerun THEN :now"
                    "   ELSE coalesce(:retry_at, run_after) END,"
                    " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                    " rerun = 0, started_at = NULL, last_error = :error "
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
                    " attempts = max(attempts - 1, 0), rerun = 0, started_at = NULL "
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
                    " rerun = 0 WHERE status = 'running' AND started_at < :cutoff"
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

    def get_page(self, job_id: str) -> FetchedPage | None:
        with Session(self.engine) as s:
            return s.get(FetchedPage, job_id)

    def save_extraction(
        self, job_id: str, data_json: str, company: str | None, role: str | None,
        deadline: str | None, prompt_version: str, status: JobStatus,
    ) -> None:  # fmt: skip
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET data_json = :data, company = :company, role = :role,
                        deadline = :deadline, prompt_version = :version, status = :status,
                        updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "data": data_json, "company": company, "role": role,
                 "deadline": deadline, "version": prompt_version, "status": status.value,
                 "now": self._now()},
            )  # fmt: skip

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

    def set_match_score(self, job_id: str, score: int | None, reason: str) -> bool:
        """Store the score; an extracted job becomes `new` (in the Inbox). True if changed."""
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    UPDATE job SET score = :score, score_reason = :reason, updated_at = :now,
                        status = CASE WHEN status = 'extracted' THEN 'new' ELSE status END
                    WHERE id = :id AND (score IS NOT :score OR score_reason IS NOT :reason
                        OR status = 'extracted')
                    """
                ),
                {"id": job_id, "score": score, "reason": reason, "now": self._now()},
            )
        return bool(result.rowcount)

    def jobs_to_match(self, version: str) -> list[str]:
        """Open jobs without answers for this profile version."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT id FROM job WHERE status IN ('extracted', 'new')
                        AND NOT EXISTS (SELECT 1 FROM match_result m
                            WHERE m.job_id = job.id AND m.profile_version = :version)
                    ORDER BY created_at
                    """
                ),
                {"version": version},
            ).all()
        return [str(r[0]) for r in rows]

    def match_answers(self, version: str) -> list[tuple[str, str]]:
        """(job_id, answers_json) for open jobs matched against this profile version."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT m.job_id, m.answers_json FROM match_result m
                    JOIN job ON job.id = m.job_id
                    WHERE m.profile_version = :version AND job.status IN ('extracted', 'new')
                    """
                ),
                {"version": version},
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

    def set_user_status(self, job_id: str, status: JobStatus) -> bool:
        """Record a Status you set in Notion. False if the job is unknown or unchanged."""
        now = self._now()
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    UPDATE job SET status = :status, user_status_at = :now, updated_at = :now,
                        score_reason = CASE WHEN :status = 'hidden' THEN 'hidden by you'
                                            ELSE score_reason END
                    WHERE id = :id AND (status != :status OR user_status_at IS NULL)
                    """
                ),
                {"id": job_id, "status": status.value, "now": now},
            )
            if not result.rowcount:
                return False
            c.execute(
                text(
                    "INSERT INTO status_change (job_id, status, changed_at) "
                    "VALUES (:id, :status, :now)"
                ),
                {"id": job_id, "status": status.value, "now": now},
            )
        return True

    def status_counts(self) -> dict[str, int]:
        """How many jobs are in each status right now."""
        with self._tx() as c:
            rows = c.execute(text("SELECT status, count(*) FROM job GROUP BY status")).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def status_changes_since(self, since: str) -> dict[str, int]:
        """How many jobs you moved into each status since this time."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, count(DISTINCT job_id) FROM status_change "
                    "WHERE changed_at >= :since GROUP BY status"
                ),
                {"since": since},
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def scores_by_status(self) -> dict[str, list[int]]:
        """Match Scores of the jobs in each status (jobs without a score are left out)."""
        with self._tx() as c:
            rows = c.execute(text("SELECT status, score FROM job WHERE score IS NOT NULL")).all()
        scores: dict[str, list[int]] = {}
        for status, score in rows:
            scores.setdefault(str(status), []).append(int(score))
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
                    "INSERT INTO alert (job_id, queued_at) VALUES (:job, :now) "
                    "ON CONFLICT (job_id) DO NOTHING"
                ),
                {"job": job_id, "now": self._now()},
            )
        return bool(result.rowcount)

    def pending_alerts(self) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text("SELECT job_id FROM alert WHERE outcome IS NULL ORDER BY queued_at")
            ).all()
        return [str(r[0]) for r in rows]

    def alerts_sent_since(self, since: str) -> int:
        """Alerts sent (or previewed in a dry run) since this time."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*) FROM alert WHERE outcome IN ('sent', 'dry_run') "
                    "AND sent_at >= :since"
                ),
                {"since": since},
            ).one()
        return int(row[0])

    def last_alert_sent_at(self) -> str | None:
        """When the latest alert email went out (or was previewed in a dry run)."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT max(sent_at) FROM alert WHERE outcome IN ('sent', 'dry_run')")
            ).one()
        return None if row[0] is None else str(row[0])

    def finish_alerts(self, job_ids: list[str], outcome: str) -> None:
        with self._tx() as c:
            for job_id in job_ids:
                c.execute(
                    text("UPDATE alert SET outcome = :outcome, sent_at = :now WHERE job_id = :job"),
                    {"job": job_id, "outcome": outcome, "now": self._now()},
                )

    # ---- digest -------------------------------------------------------------------------------

    def jobs_created_since(self, since: str) -> list[Job]:
        with Session(self.engine) as s:
            query = select(Job).where(col(Job.created_at) >= since)
            return list(s.exec(query))

    def jobs_closing(self, first_day: str, last_day: str) -> list[Job]:
        """Jobs you may still apply to whose deadline falls in these days (inclusive)."""
        open_ = [JobStatus.NEW.value, JobStatus.SHORTLISTED.value, JobStatus.RESUME_READY.value]
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status).in_(open_),
                    col(Job.deadline) >= first_day,
                    col(Job.deadline) <= last_day,
                )
                .order_by(col(Job.deadline), col(Job.score).desc())
            )
            return list(s.exec(query))

    def jobs_read_by_llm(self) -> list[Job]:
        """Jobs with extraction data, for comparing extraction methods."""
        with Session(self.engine) as s:
            query = select(Job).where(
                col(Job.data_json).is_not(None),
                col(Job.status).in_([JobStatus.EXTRACTED.value, JobStatus.DISCARDED.value]),
            )
            return list(s.exec(query))

    def jobs_missing_extraction(self) -> list[str]:
        """Jobs that reached Notion before extraction existed (for `jobradar extract`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id FROM job WHERE status = 'discovered' AND data_json IS NULL "
                    "AND canonical_url IS NOT NULL ORDER BY created_at"
                )
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
                    "SELECT count(*), sum(processed = 1 AND NOT EXISTS ("
                    " SELECT 1 FROM job_source js WHERE js.raw_message_id = m.id)) "
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
        self, since: str
    ) -> list[tuple[str, str | None, str, int, int | None, str | None, str | None]]:
        """(id, URL, status, sightings, first post id, first post text, reason) for jobs
        created since."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id, j.canonical_url, j.status, count(js.raw_message_id),"
                    " min(js.raw_message_id),"
                    " (SELECT m.text FROM job_source x"
                    "  JOIN raw_message m ON m.id = x.raw_message_id"
                    "  WHERE x.job_id = j.id ORDER BY m.posted_at, m.id LIMIT 1), "
                    " j.score_reason "
                    "FROM job j LEFT JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.created_at >= :since GROUP BY j.id ORDER BY j.created_at"
                ),
                {"since": since},
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
