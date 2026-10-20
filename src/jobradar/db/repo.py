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
from sqlmodel import Session, SQLModel, col, create_engine, func, select

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
        fingerprint: str | None = None,
    ) -> None:  # fmt: skip
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET data_json = :data, company = :company, role = :role,
                        deadline = :deadline, prompt_version = :version, status = :status,
                        fingerprint = :fingerprint, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "data": data_json, "company": company, "role": role,
                 "deadline": deadline, "version": prompt_version, "status": status.value,
                 "fingerprint": fingerprint, "now": self._now()},
            )  # fmt: skip

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

    def same_role_main(self, job_id: str, fingerprint: str, since: str) -> str | None:
        """The oldest open job with this company and role, first seen since `since` and
        before this one, that you haven't moved in Notion; None if there is none."""
        with self._tx() as c:
            row = c.execute(
                text(
                    """
                    SELECT id FROM job
                    WHERE fingerprint = :fp AND id != :id AND user_status_at IS NULL
                      AND status IN ('extracted', 'new') AND created_at >= :since
                      AND created_at < (SELECT created_at FROM job WHERE id = :id)
                    ORDER BY created_at, id LIMIT 1
                    """
                ),
                {"fp": fingerprint, "id": job_id, "since": since},
            ).first()
        return str(row[0]) if row else None

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

    def status_changes_since(self, since: str, until: str | None = None) -> dict[str, int]:
        """How many jobs you moved into each status since this time (and before `until`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, count(DISTINCT job_id) FROM status_change "
                    "WHERE changed_at >= :since AND (:until IS NULL OR changed_at < :until) "
                    "GROUP BY status"
                ),
                {"since": since, "until": until},
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

    def alerts_sent_since(self, since: str, until: str | None = None) -> int:
        """Alerts sent (or previewed in a dry run) since this time (and before `until`)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*) FROM alert WHERE outcome IN ('sent', 'dry_run') "
                    "AND sent_at >= :since AND (:until IS NULL OR sent_at < :until)"
                ),
                {"since": since, "until": until},
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

    def jobs_created_since(self, since: str, until: str | None = None) -> list[Job]:
        with Session(self.engine) as s:
            query = select(Job).where(col(Job.created_at) >= since)
            if until is not None:
                query = query.where(col(Job.created_at) < until)
            return list(s.exec(query))

    def jobs_you_moved_to(self, statuses: list[JobStatus]) -> list[Job]:
        """Jobs whose Status you set to one of these, most recent change first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status).in_([st.value for st in statuses]),
                    col(Job.user_status_at).is_not(None),
                )
                .order_by(col(Job.user_status_at).desc())
            )
            return list(s.exec(query))

    def jobs_applied(self) -> list[Job]:
        """Jobs whose Status you set to Applied (and nothing since), oldest first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status) == JobStatus.APPLIED.value,
                    col(Job.user_status_at).is_not(None),
                )
                .order_by(col(Job.user_status_at))
            )
            return list(s.exec(query))

    # ---- the dashboard's reads ------------------------------------------------------------

    def good_new_jobs(self, min_score: int, since: str) -> list[Job]:
        """New jobs scoring at least `min_score`, first seen since `since`, best first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status) == JobStatus.NEW.value,
                    col(Job.score) >= min_score,
                    col(Job.created_at) >= since,
                )
                .order_by(col(Job.score).desc(), col(Job.created_at).desc())
            )
            return list(s.exec(query))

    def jobs_in(self, statuses: list[JobStatus]) -> list[Job]:
        """Jobs in these statuses, the most recently changed first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(col(Job.status).in_([st.value for st in statuses]))
                .order_by(func.coalesce(Job.user_status_at, Job.updated_at).desc(), col(Job.id))
            )
            return list(s.exec(query))

    def search_jobs(
        self, statuses: list[str] | None, words: str, limit: int, offset: int, order: str = "new"
    ) -> tuple[list[Job], int]:
        """A page of jobs in these statuses (None: any) whose company or role contains `words`,
        and how many match in all. `order`: "new" (newest first), "old" or "score"."""
        where: list[Any] = []
        if statuses is not None:
            where.append(col(Job.status).in_(statuses))
        if words := words.strip():
            like = "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                col(Job.company).ilike(like, escape="\\") | col(Job.role).ilike(like, escape="\\")
            )
        with Session(self.engine) as s:
            total = s.exec(select(func.count()).select_from(Job).where(*where)).one()
            by: list[Any] = [col(Job.created_at).desc(), col(Job.id)]  # newest first
            if order == "old":
                by = [col(Job.created_at), col(Job.id)]
            elif order == "score":
                by = [col(Job.score).desc().nulls_last(), col(Job.created_at).desc()]
            query = select(Job).where(*where).order_by(*by).limit(limit).offset(offset)
            return list(s.exec(query)), int(total)

    # ---- full-text search (the dashboard's All jobs) ------------------------------------

    _FTS = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(job_id UNINDEXED, role, company, "
        "places, skills, body, tokenize = 'unicode61 remove_diacritics 2')"
    )

    def jobs_to_index(self, since: str | None) -> list[str]:
        """Jobs whose searchable text may have changed since `since` (all when None): the job
        itself, its page, or a new post about it."""
        with self._tx() as c:
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

    def find_jobs(
        self,
        statuses: list[str] | None,
        match: str | None,
        min_score: int | None,
        since: str | None,
        source_id: int | None,
        order: str,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[Job, str | None]], int]:
        """A page of jobs for All jobs, with a text snippet around what `match` (an FTS5
        query) found, and how many match in all. `order`: new, old, score, or relevance."""
        where: list[str] = ["1 = 1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        join = ""
        if match:
            join = "JOIN job_fts f ON f.job_id = j.id"
            where.append("job_fts MATCH :match")
            params["match"] = match
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
            "relevance": "bm25(job_fts, 0, 8, 6, 3, 4, 1)" if match else "j.created_at DESC",
        }.get(order, "j.created_at DESC, j.id")
        snippet = "snippet(job_fts, -1, char(2), char(3), '…', 14)" if match else "NULL"
        cond = " AND ".join(where)
        with self._tx() as c:
            c.execute(text(self._FTS))
            total = c.execute(text(f"SELECT count(*) FROM job j {join} WHERE {cond}"),  # noqa: S608
                              params).one()[0]  # fmt: skip
            rows = c.execute(
                text(f"SELECT j.id, {snippet} FROM job j {join} WHERE {cond} "  # noqa: S608
                     f"ORDER BY {by} LIMIT :limit OFFSET :offset"),
                params,
            ).all()  # fmt: skip
        ids = [str(r[0]) for r in rows]
        with Session(self.engine) as s:
            found = {j.id: j for j in s.exec(select(Job).where(col(Job.id).in_(ids)))}
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
                    "UNION ALL SELECT max(last_checked) FROM feed_board)"
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

    def good_job_sources(self, min_score: int, since: str) -> list[tuple[str, str, int]]:
        """(platform, chat or board title, jobs) for jobs scoring `min_score`+ first seen since
        `since`, each job counted once per source."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.platform, coalesce(s.title, s.chat_id), count(DISTINCT j.id) "
                    "FROM job j JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message m ON m.id = js.raw_message_id "
                    "JOIN source s ON s.id = m.source_id "
                    "WHERE j.created_at >= :since AND j.score >= :min "
                    "GROUP BY s.platform, s.id ORDER BY 3 DESC"
                ),
                {"since": since, "min": min_score},
            ).all()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    def search_good_count(self, min_score: int) -> int:
        """Jobs that ever scored `min_score` or more (not counting what wasn't a job)."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT count(*) FROM job WHERE score >= :min AND status != 'discarded'"),
                {"min": min_score},
            ).one()
        return int(row[0])

    def jobs_ever_in(self, statuses: list[JobStatus]) -> int:
        """Jobs that are, or once were, in one of these statuses (by your status history)."""
        query = text(
            "SELECT count(*) FROM (SELECT id FROM job WHERE status IN :sts "
            "UNION SELECT job_id FROM status_change WHERE status IN :sts)"
        ).bindparams(bindparam("sts", expanding=True))
        with self._tx() as c:
            row = c.execute(query, {"sts": [st.value for st in statuses]}).one()
        return int(row[0])

    def get_artifact(self, artifact_id: int) -> Artifact | None:
        with Session(self.engine) as s:
            return s.get(Artifact, artifact_id)

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
                    "SELECT status, changed_at FROM status_change WHERE job_id = :job "
                    "ORDER BY changed_at, id"
                ),
                {"job": job_id},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def artifacts_of(self, job_id: str) -> list[Artifact]:
        """Every resume and cover letter built for this job, newest first."""
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(Artifact.job_id == job_id)
                .order_by(col(Artifact.created_at).desc(), col(Artifact.id).desc())
            )
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

    def jobs_from_source(self, title: str) -> list[str]:
        """Jobs first or also seen in the source shown as `title` ("Workday · Accenture"),
        that you haven't moved in Notion (for `jobradar extract --again`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT DISTINCT j.id FROM job j JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message r ON r.id = js.raw_message_id "
                    "JOIN source s ON s.id = r.source_id "
                    "WHERE lower(s.title) = lower(:title) AND j.user_status_at IS NULL "
                    "AND j.status != 'discarded' AND j.canonical_url IS NOT NULL "
                    "ORDER BY j.created_at"
                ),
                {"title": title},
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
