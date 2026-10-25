"""Copy the self-hosted SQLite database into Postgres (`jobradar saas-import`).

The SQLite file is opened read-only and never changed. Rows already in Postgres are kept, so
the copy can run again safely (it only adds what's missing). The search index isn't copied:
Postgres builds its own from the copied jobs.

The self-hosted app keeps each job's score and status on `job`; here they become the owner's
`user_jobs` rows, the same way migration 0003 moves them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Integer, create_engine, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import SQLModel

from jobradar.db import models  # noqa: F401 (registers every table on SQLModel.metadata)
from jobradar.db.models import OWNER, USER_STATUSES
from jobradar.db.repo import init_db

BATCH = 1000


@dataclass(frozen=True)
class Copied:
    table: str
    in_sqlite: int
    in_postgres: int  # after the copy

    @property
    def ok(self) -> bool:
        return self.in_postgres >= self.in_sqlite


def read_only(path: Path) -> Engine:
    """An engine on the SQLite file that can't write to it."""
    if not path.is_file():
        raise FileNotFoundError(f"No database at {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def _clean(value: Any) -> Any:
    # Postgres text can't hold NUL; scraped pages occasionally contain one.
    return value.replace("\x00", "") if isinstance(value, str) else value


_MINE = {s.value for s in USER_STATUSES}
_YOURS = ("score", "score_reason", "user_status_at", "notion_page_id", "notion_synced_at")


def split_job(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """A job row from before user_jobs: (the posting, the owner's view of it or None)."""
    status = row["status"]
    mine = status in _MINE
    posting = {k: v for k, v in row.items() if k not in _YOURS}
    posting["status"] = (
        status if not mine else "extracted" if row.get("data_json") else "discovered"
    )
    posting["status_reason"] = None if mine else row.get("score_reason")
    if not mine and all(row.get(k) is None for k in _YOURS):
        return posting, None
    view = {k: row.get(k) for k in _YOURS} | {
        "user_id": OWNER, "job_id": row["id"], "status": status if mine else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }  # fmt: skip
    return posting, view


def copy(source: Engine, target: Engine) -> list[Copied]:
    """Copy every table from `source` (SQLite) to `target` (Postgres), parents first."""
    if target.dialect.name != "postgresql":
        raise ValueError("The target must be Postgres (DATABASE_URL).")
    init_db(target)
    have = set(inspect(source).get_table_names())
    old_jobs = "job" in have and "score" in {c["name"] for c in inspect(source).get_columns("job")}
    views: list[dict[str, Any]] = []  # the owner's user_jobs rows, from an old `job` table
    report = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name == "user_jobs" and old_jobs:
            for start in range(0, len(views), BATCH):
                _insert(target, table, views[start : start + BATCH])
            report.append(_count(target, table, len(views)))
            continue
        if table.name not in have:
            continue  # e.g. users and plans: the SaaS's own
        columns = {c["name"] for c in inspect(source).get_columns(table.name)}
        with source.connect() as src:
            read = select(*[c for c in table.columns if c.name in columns])
            if table.name == "job" and old_jobs:
                read = select(*[text(f'"{name}"') for name in sorted(columns)]).select_from(table)
            batch: list[dict[str, Any]] = []
            for row in src.execute(read).mappings():
                values = {k: _clean(v) for k, v in row.items()}
                if table.name == "job" and old_jobs:
                    values, view = split_job(values)
                    values = {k: v for k, v in values.items() if k in table.columns}
                    if view is not None:
                        views.append(view)
                batch.append(values)
                if len(batch) >= BATCH:
                    _insert(target, table, batch)
                    batch = []
            if batch:
                _insert(target, table, batch)
            in_sqlite = src.execute(select(func.count()).select_from(table)).scalar_one()
        report.append(_count(target, table, int(in_sqlite)))
    _reset_counters(target)
    with target.begin() as c:  # the search index is Postgres's own: rebuild it from scratch
        c.execute(text("DELETE FROM setting WHERE key = 'search_indexed_at'"))
    return report


def _count(target: Engine, table: Any, in_sqlite: int) -> Copied:
    with target.connect() as dst:
        in_pg = dst.execute(select(func.count()).select_from(table)).scalar_one()
    return Copied(table.name, in_sqlite, int(in_pg))


def _insert(target: Engine, table: Any, batch: list[dict[str, Any]]) -> None:
    with target.begin() as c:
        c.execute(insert(table).on_conflict_do_nothing(), batch)


def _reset_counters(target: Engine) -> None:
    """Integer ids were copied as they were; move each id counter past the largest one."""
    with target.begin() as c:
        for table in SQLModel.metadata.sorted_tables:
            pk = list(table.primary_key.columns)
            if len(pk) != 1 or not isinstance(pk[0].type, Integer) or not pk[0].autoincrement:
                continue
            name, column = table.name, pk[0].name
            seq = c.execute(text(f"SELECT pg_get_serial_sequence('{name}', '{column}')")).scalar()
            if seq:
                c.execute(
                    text(  # names come from the models, not from input
                        f'SELECT setval(:seq, coalesce((SELECT max("{column}") FROM "{name}"), 1), '  # noqa: S608
                        f'(SELECT max("{column}") FROM "{name}") IS NOT NULL)'
                    ),
                    {"seq": seq},
                )
