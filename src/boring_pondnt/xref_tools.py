"""The read-only xref tool for the D365 server; SQL behavior stays in xref.py."""
from __future__ import annotations
from typing import Any, Callable
from . import xref
from boring_service_kit.outcomes import Refused
from boring_service_kit.tools import READ_ONLY, as_mcp_tool

# what -> (required, optional) arguments besides `what` (D40: one tool for the four lookups)
WHAT = {
    "info": ((), ()),
    "names": (("name",), ("element_type", "limit")),
    "references": (("path",), ("direction", "kind", "module", "members", "limit", "offset")),
    "hierarchy": (("name",), ("direction", "limit")),
}


def register_tools(mcp: Any, index: xref.Xref, log: Any,
                   guard: Callable[[Any], Any], call_budget: float | None = None) -> None:
    """`guard`: the server's reader-permission check (not redaction: xref names are not secrets).
    `call_budget` (behind a Pond): one deadline for all queries of a call; None: per query only."""
    def xref_tool(what: str, name: str | None = None, path: str | None = None, element_type: str | None = None,
                  direction: str | None = None, kind: str | None = None, module: str | None = None,
                  members: bool = False, limit: int | None = None, offset: int = 0) -> dict[str, Any]:
        """Compiled references: what=info | names (name) | references (path; direction used_by|uses) | hierarchy (name; down|up)."""
        given = {"name": name, "path": path, "element_type": element_type, "direction": direction, "kind": kind,
                 "module": module, "members": members or None, "limit": limit, "offset": offset or None}
        if what not in WHAT:
            raise Refused("invalid_request", f"what is one of {', '.join(WHAT)}", what=what)
        required, optional = WHAT[what]
        allowed = set(required) | set(optional)
        extra = sorted(k for k, v in given.items() if v is not None and k not in allowed)
        missing = [k for k in required if not given[k]]
        if extra or missing:
            needs = ", ".join(required) or "nothing"
            raise Refused("invalid_request", f"what={what} needs {needs}"
                          + (f"; it also takes {', '.join(optional)}" if optional else "")
                          + (f"; not {', '.join(extra)}" if extra else ""), what=what)
        with index.deadline(call_budget):
            if what == "info":
                return index.info()
            if what == "names":
                return index.names(name, element_type, xref.NAMES_DEFAULT if limit is None else limit)
            if what == "references":
                return index.references(path, direction or "used_by", kind, module, members,
                                        xref.ROWS_DEFAULT if limit is None else limit, offset)
            return index.hierarchy(name, direction or "down", xref.CLASSES_DEFAULT if limit is None else limit)

    xref_tool.__name__ = "xref"
    mcp.tool(annotations=READ_ONLY)(as_mcp_tool(guard(xref_tool), log))
