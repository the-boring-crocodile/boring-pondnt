"""Copilot in Visual Studio refuses a tool whose input schema has an object inside
anyOf/oneOf without additionalProperties: false ("Invalid schema for function
'boring_d365web_web_set': In context=('properties', 'values', 'anyOf', '0'),
'additionalProperties' is required to be supplied and to be false", 2026-10-26),
and with one such tool the whole request fails. Open objects outside anyOf (the
core's batch operations, file_patch edits) have worked with Copilot since rc13, so
the rules tested are the observed ones, not the full strict mode. Second rule,
observed the same day: every array must name its items ("array schema missing
items", web_set's [[name, value]] pairs)."""
from __future__ import annotations

from typing import Any


def refused_objects(schema: Any, path: str = "", in_union: bool = False) -> list[str]:
    """Paths of object schemas inside anyOf/oneOf that are not closed."""
    found: list[str] = []
    if isinstance(schema, dict):
        is_object = schema.get("type") == "object" or "properties" in schema
        if in_union and is_object and schema.get("additionalProperties") is not False:
            found.append(path or "/")
        if schema.get("type") == "array" and "items" not in schema:
            found.append((path or "/") + " (array without items)")
        for key, value in schema.items():
            found += refused_objects(value, f"{path}/{key}", in_union or key in ("anyOf", "oneOf"))
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found += refused_objects(value, f"{path}[{i}]", in_union)
    return found


def assert_copilot_accepts(tools: Any) -> None:
    bad = {t.name: refused_objects(getattr(t, "inputSchema", None) or t.input_schema) for t in tools}
    bad = {name: paths for name, paths in bad.items() if paths}
    assert not bad, f"tool schemas Copilot refuses: {bad}"
