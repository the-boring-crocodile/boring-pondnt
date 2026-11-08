"""The read-only xref tool for the D365 server; SQL behavior stays in xref.py."""
from __future__ import annotations
from typing import Annotated, Any, Callable
from pydantic import Field
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
    def xref_tool(
        what: Annotated[str, Field(
            description="info lists index details. names and hierarchy need name. references needs path.",
            json_schema_extra={"enum": tuple(WHAT)},
        )],
        name: Annotated[str | None, Field(
            description=(
                "For names, give an element, label ID or path prefix ending in *. For hierarchy, "
                "give a class or interface name."
            ),
        )] = None,
        path: Annotated[str | None, Field(
            description="For references, use the exact path returned by names, or a label ID.",
        )] = None,
        element_type: Annotated[str | None, Field(
            description=(
                "For names, narrow an element name to a folder or type such as Classes, Tables or "
                "DataEntityView. Surrounding slashes are accepted."
            ),
        )] = None,
        direction: Annotated[str | None, Field(
            description=(
                "For references, used_by (default) or uses. For hierarchy, down (default) or up. "
                "Empty uses the default."
            ),
        )] = None,
        kind: Annotated[str | None, Field(
            description="Reference kind such as call or extends. The info result also lists the kinds.",
            json_schema_extra={"enum": (*xref.KIND_IDS, None)},
        )] = None,
        module: Annotated[str | None, Field(description="Filter references by package name.")] = None,
        members: Annotated[bool, Field(description="For references, include names below the path.")] = False,
        limit: Annotated[int | None, Field(
            description="Maximum rows. names and references allow up to 500, hierarchy up to 1,000.",
            json_schema_extra={"minimum": 1, "maximum": xref.CLASSES_MAX},
        )] = None,
        offset: Annotated[int, Field(
            description=(
                "Skip this many reference rows. Add rows shown to continue. Offset plus limit must "
                "be at most 10,000."
            ),
            json_schema_extra={"minimum": 0},
        )] = 0,
    ) -> dict[str, Any]:
        ("Read compiled D365 references. Find a path with names before asking for references. Use hierarchy "
         "for classes.")
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
