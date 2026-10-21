"""Read-only xref registrations for the D365 server; SQL behavior stays in xref.py."""
from __future__ import annotations
import functools
from typing import Any, Callable
from . import xref
from boring_service_kit.tools import READ_ONLY, as_mcp_tool


def register_tools(mcp: Any, index: xref.Xref, log: Any,
                   guard: Callable[[Any], Any], call_budget: float | None = None) -> None:
    """`guard`: the server's reader-permission check (not redaction: xref names are not secrets).
    `call_budget` (behind a Pond): one deadline for all queries of a call; None: per query only."""
    def xref_info() -> dict[str, Any]:
        """The database: version, providers, counts of names and references, the reference kinds."""
        return index.info()

    def xref_names(name: str, element_type: str | None = None, limit: int = xref.NAMES_DEFAULT) -> dict[str, Any]:
        """Paths for an element name or label ID (CustTable, @SYS1), or below a prefix (/Classes/Sales*)."""
        return index.names(name, element_type, limit)

    def xref_references(path: str, direction: str = "used_by", kind: str | None = None, module: str | None = None,
                        members: bool = False, limit: int = xref.ROWS_DEFAULT, offset: int = 0) -> dict[str, Any]:
        """used_by|uses of a path or label: totals by kind and module (package, not model), then rows. members: its children too."""
        return index.references(path, direction, kind, module, members, limit, offset)

    def xref_hierarchy(name: str, direction: str = "down", limit: int = xref.CLASSES_DEFAULT) -> dict[str, Any]:
        """down: all classes extending (or implementing) a class or interface; up: its bases and interfaces."""
        return index.hierarchy(name, direction, limit)

    def bounded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            with index.deadline(call_budget):
                return fn(*args, **kwargs)
        return call

    for fn in (xref_info, xref_names, xref_references, xref_hierarchy):
        mcp.tool(annotations=READ_ONLY)(as_mcp_tool(guard(bounded(fn)), log))
