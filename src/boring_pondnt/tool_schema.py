"""Parameter types whose advertised JSON schema Copilot in Visual Studio accepts.

Copilot refuses an object inside anyOf unless it is closed (additionalProperties:
false), which a free-form map cannot be; pydantic writes `dict | None` and unions as
anyOf. These keep the validation of the Python type and advertise one plain schema
instead (tests/strict_schema.py holds the observed rules). Every array names its
items: Copilot refuses "array schema missing items" too (web_set, 2026-10-26).
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import WithJsonSchema

# {name: text}, optional
TextMap = Annotated[dict[str, str] | None, WithJsonSchema({"type": "object", "additionalProperties": {"type": "string"}})]
# {name: any value}, optional
AnyMap = Annotated[dict[str, Any] | None, WithJsonSchema({"type": "object"})]
# [{"field": name, "value": text}, ...] advertised: a flat list of closed objects, the one
# form Copilot takes (it refused an open object in anyOf, then an array inside an array,
# 2026-10-26). An object {name: value} and [[name, value], ...] are still accepted.
NameValuePairs = Annotated[dict[str, Any] | list[list[Any] | dict[str, Any]],
                           WithJsonSchema({"type": "array",
                                           "items": {"type": "object",
                                                     "properties": {"field": {"type": "string"},
                                                                    "value": {"type": "string"}},
                                                     "required": ["field", "value"],
                                                     "additionalProperties": False},
                                           "description": "[{field, value}, ...] in the order to type them"})]
