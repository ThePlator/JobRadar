"""All database queries (swap SQLite/Postgres here).

Every method opens its own short transaction. Task-queue state changes are single SQL
statements, so they stay atomic even if the process dies between calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event, text
from sqlalchemy.engine import Connection
from sqlmodel import Session, SQLModel, create_engine, select

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db import models  # noqa: F401  (registers tables on SQLModel.metadata)
from jobradar.db.models import Job, JobStatus, Platform, RawMessage, Source, TaskStatus

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
        self, type_: str, key: str, payload: dict[str, Any] | None = None, run_after: str = ""
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
                    "INSERT INTO task (type, key, payload, status, attempts, run_after, rerun) "
                    "VALUES (:type, :key, :payload, 'pending', 0, :run_after, 0) "
                    "ON CONFLICT (type, key) DO UPDATE SET "
                    " payload = excluded.payload,"
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
                },
            )

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        """Atomically take the oldest ready task (optionally of the given types)."""
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
                f"{type_filter} ORDER BY run_after, id LIMIT 1) "
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
    ) -> list[tuple[str, str | None, str, int, int | None, str | None]]:
        """(id, URL, status, sightings, first post id, first post text) for jobs created since."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id, j.canonical_url, j.status, count(js.raw_message_id),"
                    " min(js.raw_message_id),"
                    " (SELECT m.text FROM job_source x"
                    "  JOIN raw_message m ON m.id = x.raw_message_id"
                    "  WHERE x.job_id = j.id ORDER BY m.posted_at, m.id LIMIT 1) "
                    "FROM job j LEFT JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.created_at >= :since GROUP BY j.id ORDER BY j.created_at"
                ),
                {"since": since},
            ).all()
        return [(str(r[0]), r[1], str(r[2]), int(r[3]), r[4], r[5]) for r in rows]

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
