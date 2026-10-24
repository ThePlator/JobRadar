"""What someone typed into All jobs' search, as a query each database understands.

`services.search.match` turns the words into a `TextQuery`; `Repo.find_jobs` compiles it to
SQLite FTS5 or Postgres `tsquery`. Words are matched as prefixes ("kube" finds Kubernetes),
all of them required; a place only in the places field; remote means remote or hybrid.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextQuery:
    words: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    remote: bool = False

    def fts5(self) -> str:
        """The SQLite FTS5 MATCH expression."""

        def terms(ws: list[str]) -> list[str]:
            return [f'"{w.replace(chr(34), "")}"*' for w in ws]

        parts = terms(self.words)
        if self.places:
            parts.append("places : (" + " ".join(terms(self.places)) + ")")
        if self.remote:
            parts.append('places : ("remote" OR "hybrid")')
        return " AND ".join(parts)

    @staticmethod
    def _tsquery(ws: list[str], joiner: str = " & ") -> str:
        """Prefix lexemes for to_tsquery('simple', ...), quoted so no input can become an
        operator."""
        return joiner.join("'" + w.lower().replace("'", "''").replace("\\", "") + "':*" for w in ws)

    def words_tsquery(self) -> str | None:
        return self._tsquery(self.words) if self.words else None

    def places_tsquery(self) -> str | None:
        parts = [f"({self._tsquery(self.places, ' & ')})"] if self.places else []
        if self.remote:
            parts.append("('remote' | 'hybrid')")
        return " & ".join(parts) or None
