"""Add and remove boards in config.yaml as text, so your comments and layout stay as they are.

Boards live in `sources.boards`, one section per hiring system:

    boards:
      greenhouse:
        Stripe:     stripe

A new board goes at the end of its section (the section is added if it's missing), lined up
with the entries above it. Anything unusual, such as a section written as `[a, b]`, is left
for you to edit by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from jobradar.config import board_from_link

_KEY = re.compile(r"^(\s*)([^\s#:][^:#]*?):\s*(.*?)\s*(#.*)?$")
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&()+'-]*$")


class EditError(Exception):
    """config.yaml can't be edited safely; the message says what to do instead."""


@dataclass
class _Line:
    indent: int
    key: str | None  # None for blank and comment lines
    value: str


def _parse(line: str) -> _Line:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return _Line(0, None, "")
    m = _KEY.match(line)
    if not m:
        return _Line(len(line) - len(line.lstrip()), "", stripped)
    key = m.group(2).strip().strip("'\"")
    return _Line(len(m.group(1)), key, m.group(3).strip().strip("'\""))


def _block(lines: list[str], start: int) -> int:
    """Index just past the entries nested under the key on line `start`."""
    indent = _parse(lines[start]).indent
    end = start + 1
    last = start + 1
    while end < len(lines):
        parsed = _parse(lines[end])
        if parsed.key is not None:
            if parsed.indent <= indent:
                break
            last = end + 1
        end += 1
    return last


def _find(lines: list[str], key: str, start: int, stop: int, indent_above: int) -> int | None:
    """The line of `key:` directly nested in lines[start:stop] (indent > indent_above)."""
    child_indent: int | None = None
    for i in range(start, stop):
        parsed = _parse(lines[i])
        if parsed.key is None or parsed.indent <= indent_above:
            continue
        child_indent = parsed.indent if child_indent is None else child_indent
        if parsed.indent == child_indent and parsed.key == key:
            return i
    return None


def _boards_section(lines: list[str]) -> tuple[int, int]:
    sources = _find(lines, "sources", 0, len(lines), -1)
    if sources is None:
        raise EditError("config.yaml has no `sources:` section; add `sources:` / `boards:` first")
    boards = _find(
        lines, "boards", sources + 1, _block(lines, sources), _parse(lines[sources]).indent
    )
    if boards is None:
        raise EditError(
            "config.yaml has no `sources: boards:` section; add `boards:` under sources"
        )
    if _parse(lines[boards]).value:
        raise EditError("`boards:` is written on one line in config.yaml; edit it by hand")
    return boards, _block(lines, boards)


def _value_board(value: str) -> str:
    if "/" not in value:
        return value
    try:
        return board_from_link(value)[1]
    except ValueError:  # e.g. an Oracle "host/SiteName", written as is
        return value


def add_board(text: str, ats: str, board: str, name: str) -> str:
    lines = text.splitlines()
    boards, end = _boards_section(lines)
    boards_indent = _parse(lines[boards]).indent
    section = _find(lines, ats, boards + 1, end, boards_indent)
    shown = name if _PLAIN_NAME.match(name) else json.dumps(name)
    if section is None:
        step = _child_indent(lines, boards + 1, end, boards_indent) or boards_indent + 2
        new = [f"{' ' * step}{ats}:", f"{' ' * (step + 2)}{shown}: {board}"]
        lines[end:end] = new
        return "\n".join(lines) + "\n"
    head = _parse(lines[section])
    if head.value:
        raise EditError(f"`{ats}:` is written on one line in config.yaml; edit it by hand")
    stop = _block(lines, section)
    entries = [i for i in range(section + 1, stop) if _parse(lines[i]).key is not None]
    for i in entries:
        parsed = _parse(lines[i])
        if _value_board(parsed.value).casefold() == board.casefold():
            raise EditError(f"{ats} board '{board}' is already in config.yaml (as {parsed.key})")
        if parsed.key.casefold() == name.casefold():  # type: ignore[union-attr]
            raise EditError(f"there's already a {ats} board named '{name}'; pick another --name")
    indent = _parse(lines[entries[0]]).indent if entries else head.indent + 2
    # Line the board name up with the entries above when they are lined up.
    column = _value_column(lines[entries[-1]]) if entries else 0
    entry = f"{' ' * indent}{shown}:"
    lines.insert(stop, entry.ljust(column - 1) + " " + board)
    return "\n".join(lines) + "\n"


def _value_column(line: str) -> int:
    m = re.match(r"^(\s*(?:\"[^\"]*\"|'[^']*'|[^:#]+?):\s*)", line)
    return len(m.group(1)) if m else 0


def _child_indent(lines: list[str], start: int, stop: int, above: int) -> int | None:
    for i in range(start, stop):
        parsed = _parse(lines[i])
        if parsed.key is not None and parsed.indent > above:
            return parsed.indent
    return None


def remove_board(text: str, which: str, ats: str | None = None) -> tuple[str, str, str]:
    """Remove the board whose name or board name is `which` (or a link to it). Returns the
    new text and the (hiring system, name) removed."""
    if "/" in which:
        ats, which = board_from_link(which)
    lines = text.splitlines()
    boards, end = _boards_section(lines)
    boards_indent = _parse(lines[boards]).indent
    matches: list[tuple[int, str, str]] = []
    for section in range(boards + 1, end):
        head = _parse(lines[section])
        if head.key is None or head.indent != _child_indent(lines, boards + 1, end, boards_indent):
            continue
        if ats and head.key != ats:
            continue
        for i in range(section + 1, _block(lines, section)):
            parsed = _parse(lines[i])
            if parsed.key is None:
                continue
            hit = which.casefold() in (parsed.key.casefold(), _value_board(parsed.value).casefold())
            if hit:
                matches.append((i, head.key, parsed.key))
    if not matches:
        raise EditError(f"no board '{which}' in config.yaml; `jobradar boards` lists them")
    if len(matches) > 1:
        found = ", ".join(f"{a}: {n}" for _, a, n in matches)
        raise EditError(f"'{which}' matches several boards ({found}); add --ats to pick one")
    index, section_ats, name = matches[0]
    del lines[index]
    return "\n".join(lines) + "\n", section_ats, name
