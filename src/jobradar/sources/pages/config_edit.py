"""Add and remove watched pages in config.yaml as text, so comments and layout stay as they are.

Pages live in `sources.pages.watch`:

    pages:
      watch:
        TCS NextStep: https://…

`pages:` and `watch:` are added at the end of `sources:` when missing. The same line-based
helpers as the boards editor are used; anything unusual is left for you to edit by hand.
"""

from __future__ import annotations

import json

from jobradar.sources.boards.config_edit import (
    _PLAIN_NAME,
    EditError,
    _block,
    _child_indent,
    _find,
    _parse,
    _value_column,
)


def _section(lines: list[str], parent: int, key: str) -> int | None:
    found = _find(lines, key, parent + 1, _block(lines, parent), _parse(lines[parent]).indent)
    if found is not None and _parse(lines[found]).value == "{}":  # `watch: {}`, as shipped
        lines[found] = lines[found].replace(" {}", "", 1)
    if found is not None and _parse(lines[found]).value:
        raise EditError(f"`{key}:` is written on one line in config.yaml; edit it by hand")
    return found


def _add_child(lines: list[str], parent: int, key: str) -> int:
    """Insert `key:` as the last child of the key on line `parent`; returns its line."""
    above = _parse(lines[parent]).indent
    end = _block(lines, parent)
    step = _child_indent(lines, parent + 1, end, above) or above + 2
    lines.insert(end, f"{' ' * step}{key}:")
    return end


def _watch(lines: list[str], create: bool) -> int | None:
    sources = _find(lines, "sources", 0, len(lines), -1)
    if sources is None:
        raise EditError("config.yaml has no `sources:` section; add `sources:` first")
    pages = _section(lines, sources, "pages")
    if pages is None:
        if not create:
            return None
        pages = _add_child(lines, sources, "pages")
    watch = _section(lines, pages, "watch")
    if watch is None and create:
        watch = _add_child(lines, pages, "watch")
    return watch


def add_page(text: str, name: str, url: str) -> str:
    lines = text.splitlines()
    watch = _watch(lines, create=True)
    if watch is None:  # unreachable: create=True always adds the section
        raise EditError("couldn't find or add `sources: pages: watch:` in config.yaml")
    stop = _block(lines, watch)
    entries = [i for i in range(watch + 1, stop) if _parse(lines[i]).key is not None]
    for i in entries:
        parsed = _parse(lines[i])
        if parsed.value.rstrip("/") == url.rstrip("/"):
            raise EditError(f"that page is already watched (as {parsed.key})")
        if parsed.key.casefold() == name.casefold():  # type: ignore[union-attr]
            raise EditError(f"there's already a page named '{name}'; pick another name")
    indent = _parse(lines[entries[0]]).indent if entries else _parse(lines[watch]).indent + 2
    shown = name if _PLAIN_NAME.match(name) else json.dumps(name)
    column = _value_column(lines[entries[-1]]) if entries else 0
    entry = f"{' ' * indent}{shown}:"
    lines.insert(stop, entry.ljust(column - 1) + " " + url)
    return "\n".join(lines) + "\n"


def remove_page(text: str, which: str) -> tuple[str, str]:
    """Remove the page whose name or address is `which`. Returns the new text and its name."""
    lines = text.splitlines()
    watch = _watch(lines, create=False)
    if watch is None:
        raise EditError("no pages are watched in config.yaml")
    hits = []
    for i in range(watch + 1, _block(lines, watch)):
        parsed = _parse(lines[i])
        if parsed.key is None:
            continue
        if which.casefold() == parsed.key.casefold() or which.rstrip("/") == parsed.value.rstrip(
            "/"
        ):
            hits.append((i, parsed.key))
    if not hits:
        raise EditError(f"no page '{which}' in config.yaml; `jobradar pages` lists them")
    index, name = hits[0]
    del lines[index]
    return "\n".join(lines) + "\n", name
