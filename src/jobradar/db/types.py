"""Column types for times and JSON: real `timestamptz` and `jsonb` on Postgres (checked by the
database, and fast to sort and index), plain text on SQLite, as before.

The rest of JobRadar keeps seeing what it always has, strings: times as UTC ISO text in
`clock.to_iso`'s format, JSON as text. On Postgres, `use_text_values` makes the driver hand
back both that way, so every query (written in SQL or not) reads the same on both databases.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from jobradar.clock import to_iso


class IsoTime(TypeDecorator[str]):
    """A moment: `timestamptz` on Postgres, ISO text on SQLite; a string to Python."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(TIMESTAMP(timezone=True))
        return dialect.type_descriptor(String())

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        return to_iso(value) if isinstance(value, datetime) else value


class JsonText(TypeDecorator[str]):
    """JSON: `jsonb` on Postgres, text on SQLite; a string to Python either way."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(String())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        # JSONB writes Python values as JSON; ours arrive already written as JSON text.
        if dialect.name == "postgresql" and isinstance(value, str):
            return json.loads(value)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None or isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)


def use_text_values(dbapi_connection: Any) -> None:
    """Postgres: read `timestamptz` back as ISO text (UTC, `to_iso`'s format) and `jsonb` as
    its JSON text, for one psycopg connection."""
    from psycopg.adapt import Loader

    class TimeLoader(Loader):
        def load(self, data: Any) -> str:
            text = bytes(data).decode()
            return to_iso(datetime.fromisoformat(text))

    class JsonLoader(Loader):
        def load(self, data: Any) -> str:
            return bytes(data).decode()

    dbapi_connection.adapters.register_loader("timestamptz", TimeLoader)
    dbapi_connection.adapters.register_loader("jsonb", JsonLoader)
