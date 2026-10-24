"""Copy the self-hosted SQLite database into Postgres (`jobradar saas-import`).

The SQLite file is opened read-only and never changed. Rows already in Postgres are kept, so
the copy can run again safely (it only adds what's missing). The search index isn't copied:
Postgres builds its own from the copied jobs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Integer, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import SQLModel

from jobradar.db import models  # noqa: F401 (registers every table on SQLModel.metadata)
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


def copy(source: Engine, target: Engine) -> list[Copied]:
    """Copy every table from `source` (SQLite) to `target` (Postgres), parents first."""
    if target.dialect.name != "postgresql":
        raise ValueError("The target must be Postgres (DATABASE_URL).")
    init_db(target)
    report = []
    for table in SQLModel.metadata.sorted_tables:
        with source.connect() as src:
            rows = src.execute(select(table)).mappings()
            batch: list[dict[str, Any]] = []
            for row in rows:
                batch.append({k: _clean(v) for k, v in row.items()})
                if len(batch) >= BATCH:
                    _insert(target, table, batch)
                    batch = []
            if batch:
                _insert(target, table, batch)
            in_sqlite = src.execute(select(func.count()).select_from(table)).scalar_one()
        with target.connect() as dst:
            in_pg = dst.execute(select(func.count()).select_from(table)).scalar_one()
        report.append(Copied(table.name, int(in_sqlite), int(in_pg)))
    _reset_counters(target)
    with target.begin() as c:  # the search index is Postgres's own: rebuild it from scratch
        c.execute(text("DELETE FROM setting WHERE key = 'search_indexed_at'"))
    return report


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
