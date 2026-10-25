"""The API's reference page, made from its own OpenAPI document: server-rendered like every
other page (no script from elsewhere), so it always matches what the API does."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True)
class Param:
    name: str
    where: str  # query or path
    required: bool
    kind: str
    about: str


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    summary: str
    about: str
    params: list[Param]
    answer: str  # the response's schema name, or what it sends


@dataclass
class Group:
    name: str
    endpoints: list[Endpoint] = field(default_factory=list)


def _kind(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " or ".join(_kind(s) for s in schema["anyOf"] if s.get("type") != "null")
    if schema.get("type") == "array":
        return f"list of {_kind(schema.get('items', {}))}"
    if "enum" in schema:
        return " | ".join(str(v) for v in schema["enum"])
    return str(schema.get("type", "any"))


def reference(api: FastAPI, prefix: str = "/api/v1") -> list[Group]:
    doc = api.openapi()
    groups: dict[str, Group] = {}
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            ok = op.get("responses", {}).get("200", {}).get("content", {})
            answer = "PDF" if "application/pdf" in ok else _kind(
                ok.get("application/json", {}).get("schema", {}))  # fmt: skip
            summary, _, about = (op.get("description") or op.get("summary", "")).partition("\n")
            params = [
                Param(p["name"], p["in"], p.get("required", False), _kind(p.get("schema", {})),
                      p.get("description") or p.get("schema", {}).get("description", ""))
                for p in op.get("parameters", [])
            ]  # fmt: skip
            tag = (op.get("tags") or ["other"])[0]
            groups.setdefault(tag, Group(tag)).endpoints.append(
                Endpoint(
                    method.upper(),
                    prefix + path,
                    summary.strip(),
                    " ".join(about.split()),
                    params,
                    answer,
                )
            )
    return list(groups.values())
