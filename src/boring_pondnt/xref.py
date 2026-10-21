"""Read-only queries on a D365 cross-reference database (DYNAMICSXREFDB), the one Visual Studio's
"Find references" reads. Filled by the compiler (provider Xppc.exe: code) and by the metadata
provider (properties such as a field's label or a relation's table).

Schema (version 1.0.0.10): Names(Id, Path, ProviderId, ModuleId),
References(SourceId, TargetId, Kind, Line, Column), Modules(Id, Module), Providers(Id, Provider),
DBVersion(Version); indexes on Names.Path and on References.SourceId / TargetId, none on Kind.

Paths have two shapes:
- code: `/Classes/SalesFormLetter/Methods/run`, `/Tables/CustTable/Fields/AccountNum`, `/Labels/@SYS7149`
- metadata: `Table/SalesLine/TableRelation/CustTable?RelatedTable`, `Enum/ABC/EnumValue/A?Label`
  (the part after `?` names the property).

Every query looks names up by exact path or by a path range first (index seeks), then references
by those ids: a `LIKE '%x'` on 5 million names takes 18 s, a seek well under a second.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from . import sqlserver
from boring_service_kit.outcomes import Failed, Refused

KINDS = {1: "call", 2: "type", 3: "implements", 4: "extends", 5: "test_call", 6: "property",
         7: "attribute", 8: "test_helper_call", 9: "tag", 10: "override"}
KIND_IDS = {name: number for number, name in KINDS.items()}
ROWS_DEFAULT, ROWS_MAX = 50, 500
PAGE_END_MAX = 10_000  # offset + limit: deeper paging means narrowing (kind, module) instead
NAMES_DEFAULT, NAMES_MAX = 50, 500
IDS_PER_QUERY = 1000  # SQL Server takes at most 2100 parameters
MEMBERS_MAX = 20_000
HIERARCHY_DEPTH = 30
CLASSES_DEFAULT, CLASSES_MAX = 100, 1000
STALL_SECONDS_DEFAULT = 120.0

# Element folders in code paths (Xppc) and element types in metadata paths, for a bare name.
CODE_FOLDERS = ("Classes", "Tables", "Edts", "Enums", "Views", "DataEntityViews", "Maps", "Queries", "Forms",
                "MenuItemDisplays", "MenuItemActions", "MenuItemOutputs", "Reports", "SecurityPrivileges",
                "SecurityDuties", "SecurityRoles", "Menus", "Tiles", "Interfaces", "ClrType")
METADATA_TYPES = ("Table", "Form", "DataEntityView", "View", "QuerySimple", "Enum", "Report", "Map", "Menu",
                  "MenuItemDisplay", "MenuItemAction", "MenuItemOutput", "SecurityPrivilege", "SecurityDuty",
                  "SecurityRole", "Class", "EdtString", "EdtEnum", "EdtReal", "EdtInt", "EdtInt64", "EdtDate",
                  "EdtUtcDateTime", "EdtGuid", "EdtContainer", "EdtTime", "EdtDateTime", "Tile",
                  "TableExtension", "FormExtension", "SecurityDutyExtension", "AggregateMeasurement",
                  "AggregateDimension")
_NAME = re.compile(r"[A-Za-z0-9_@:.\-]{1,200}")
_PATH = re.compile(r"[^\x00-\x1f%\[\]]{1,800}")


class Dialect:
    """The few differences between SQL Server and the SQLite used by the tests."""

    def __init__(self, name: str) -> None:
        self.name = name

    def top(self, n: int) -> tuple[str, str]:
        n = int(n)
        return (f"TOP ({n}) ", "") if self.name == "mssql" else ("", f" LIMIT {n}")

    @property
    def binary(self) -> str:
        """Order by code points, as Python does: SQL Server's collation ignores case (and weighs '_'
        and '-' its own way), so a page cut in SQL and merged in Python would skip or repeat rows."""
        return " COLLATE Latin1_General_BIN2" if self.name == "mssql" else " COLLATE BINARY"


def odbc_connector(server: str, database: str, driver: str | None = None,
                   timeout: float = STALL_SECONDS_DEFAULT) -> Callable[[], Any]:
    """Windows authentication, no password; READ UNCOMMITTED: Visual Studio rewrites the database while
    it builds -- never block it, never wait for it."""
    return sqlserver.odbc_connector(server, database, driver, timeout, app="boring-pondnt-xref",
                                    read_uncommitted=True)


ESCAPE = "ESCAPE '!'"


def like_prefix(text: str) -> str:
    """A LIKE pattern (escape character !) for everything starting with `text`."""
    return re.sub(r"([!%_\[])", r"!\1", text) + "%"


def label_path(text: str) -> str | None:
    return f"/Labels/{text}" if text.startswith("@") else None


class Xref:
    def __init__(self, connect: Callable[[], Any], dialect: str = "mssql") -> None:
        self._connect = connect
        self._local = threading.local()
        self.sql = Dialect(dialect)
        self.source = getattr(connect, "description", dialect)

    # -- plumbing ---------------------------------------------------------------------------------

    @contextmanager
    def deadline(self, seconds: float | None) -> Iterator[None]:
        """One deadline for all queries of one tool call: none starts after it.
        The stall timeout still bounds each query; None: only that."""
        self._local.deadline = time.monotonic() + seconds if seconds else None
        try:
            yield
        finally:
            self._local.deadline = None

    def _cursor(self) -> Any:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            try:
                connection = self._connect()
            except Exception as error:  # noqa: BLE001 -- the driver's own exception types
                raise Failed("unavailable", f"cannot open the cross-reference database: {_first_line(error)}",
                             source=self.source) from None
            self._local.connection = connection
        return connection.cursor()

    def _query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        deadline = getattr(self._local, "deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise Failed("timeout", "the call's time ran out before its next query; "
                         "narrow it (kind, module, limit)")
        try:
            cursor = self._cursor()
            cursor.execute(sql, list(params))
            return [tuple(row) for row in cursor.fetchall()]
        except Failed:
            raise
        except Exception as error:  # noqa: BLE001
            broken, self._local.connection = getattr(self._local, "connection", None), None  # never reused
            try:
                if broken is not None:
                    broken.close()
            except Exception:  # noqa: BLE001
                pass
            message = _first_line(error)
            if "timeout" in message.lower() or "HYT00" in message:
                raise Failed("timeout", "the query stalled and was stopped; narrow it (kind, module, limit)") from None
            raise Failed("query_failed", message) from None

    def _ids(self, path: str, members: bool) -> list[int]:
        """Name ids for a path, and with members everything below it (`path/...`): index seeks only."""
        sql = "SELECT Id FROM Names WHERE Path = ?"
        params: list[Any] = [path]
        if members:  # children (`path/...`) and metadata properties (`path?Label`); literal prefixes seek
            sql += f" OR Path LIKE ? {ESCAPE} OR Path LIKE ? {ESCAPE}"
            params += [like_prefix(path + "/"), like_prefix(path + "?")]
            top, limit = self.sql.top(MEMBERS_MAX + 1)
            sql = sql.replace("SELECT ", f"SELECT {top}", 1) + limit
        ids = [row[0] for row in self._query(sql, params)]
        if len(ids) > MEMBERS_MAX:
            raise Refused("too_broad", f"more than {MEMBERS_MAX} names below this path; pick a smaller one",
                          path=path)
        return ids

    def _chunks(self, ids: list[int]) -> list[list[int]]:
        return [ids[i:i + IDS_PER_QUERY] for i in range(0, len(ids), IDS_PER_QUERY)]

    # -- tools ------------------------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        started = time.monotonic()
        version = self._query("SELECT Version FROM DBVersion")
        providers = [row[0] for row in self._query("SELECT Provider FROM Providers ORDER BY Id")]
        if self.sql.name == "mssql":  # row counts from the catalog: counting 21 million rows takes long
            counts = dict(self._query(
                "SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id "
                "AND p.index_id IN (0, 1) WHERE t.name IN ('Names', 'References', 'Modules') GROUP BY t.name"))
        else:
            counts = {table: self._query(f"SELECT COUNT(*) FROM [{table}]")[0][0]
                      for table in ("Names", "References", "Modules")}
        return {"source": self.source, "version": version[0][0] if version else None, "providers": providers,
                "names": int(counts.get("Names", 0)), "references": int(counts.get("References", 0)),
                "modules": int(counts.get("Modules", 0)), "kinds": list(KIND_IDS),
                "seconds": round(time.monotonic() - started, 3)}

    def names(self, name: str, element_type: str | None = None, limit: int = NAMES_DEFAULT) -> dict[str, Any]:
        """Paths for an element name (`CustTable`, `@SYS7149`), or below a path prefix (`/Classes/Sales*`)."""
        _bounded("limit", limit, NAMES_MAX)
        top, tail = self.sql.top(limit + 1)
        if name.startswith("/") or "/" in name:
            prefix = name[:-1] if name.endswith("*") else name
            if not _PATH.fullmatch(prefix) or "*" in prefix:
                raise Refused("invalid_request", "a path, optionally ending in * (no other wildcards)", name=name)
            if name.endswith("*"):
                rows = self._query(f"SELECT {top}n.Path, m.Module FROM Names n JOIN Modules m ON m.Id = n.ModuleId "
                                   f"WHERE n.Path LIKE ? {ESCAPE} ORDER BY n.Path{tail}", [like_prefix(prefix)])
            else:
                rows = self._query("SELECT n.Path, m.Module FROM Names n JOIN Modules m ON m.Id = n.ModuleId "
                                   "WHERE n.Path = ?", [prefix])
        else:
            if not _NAME.fullmatch(name):
                raise Refused("invalid_request", "an element name (letters, digits, _), a label ID (@File:Id), "
                              "or a path starting with /", name=name)
            label = label_path(name)
            if label:
                candidates = [label]
            elif element_type:
                folder = element_type.strip("/")
                candidates = [f"/{folder}/{name}"] if folder in CODE_FOLDERS else [f"{folder}/{name}"]
                if folder not in CODE_FOLDERS and folder not in METADATA_TYPES:
                    raise Refused("invalid_request", "unknown element_type",
                                  code_folders=list(CODE_FOLDERS), metadata_types=list(METADATA_TYPES))
            else:
                candidates = [f"/{folder}/{name}" for folder in CODE_FOLDERS] + \
                             [f"{kind}/{name}" for kind in METADATA_TYPES]
            marks = ",".join("?" * len(candidates))
            rows = self._query(f"SELECT {top}n.Path, m.Module FROM Names n JOIN Modules m ON m.Id = n.ModuleId "
                               f"WHERE n.Path IN ({marks}) ORDER BY n.Path{tail}", candidates)
        found = [{"path": path, "module": module} for path, module in rows[:limit]]
        result: dict[str, Any] = {"query": name, "names": found, "shown": len(found), "truncated": len(rows) > limit}
        if not found:
            result["hint"] = "nothing by that exact name; check the spelling or try a path prefix: /Classes/Name*"
        return result

    def references(self, path: str, direction: str = "used_by", kind: str | None = None,
                   module: str | None = None, members: bool = False, limit: int = ROWS_DEFAULT,
                   offset: int = 0) -> dict[str, Any]:
        """used_by: what references `path`; uses: what `path` references. Totals by kind and module, then rows."""
        if direction not in ("used_by", "uses"):
            raise Refused("invalid_request", "direction is used_by or uses", direction=direction)
        _bounded("limit", limit, ROWS_MAX)
        if offset < 0:
            raise Refused("invalid_request", "offset must be 0 or more")
        if offset + limit > PAGE_END_MAX:
            raise Refused("invalid_request", f"offset + limit must stay within {PAGE_END_MAX}; "
                          "narrow by kind or module instead of paging that deep")
        path = label_path(path) or path
        if not _PATH.fullmatch(path):
            raise Refused("invalid_request", "path as xref_names returns it, e.g. /Classes/Name or @SYS1", path=path)
        kind_id = None
        if kind is not None:
            if kind not in KIND_IDS:
                raise Refused("invalid_request", "unknown kind", kind=kind, kinds=list(KIND_IDS))
            kind_id = KIND_IDS[kind]
        started = time.monotonic()
        ids = self._ids(path, members)
        if not ids:
            raise Refused("not_found", "no such name in the cross-reference database; find it with xref_names",
                          path=path)
        mine, other = ("TargetId", "SourceId") if direction == "used_by" else ("SourceId", "TargetId")
        where = ""
        extra: list[Any] = []
        if kind_id is not None:
            where += " AND r.Kind = ?"
            extra.append(kind_id)
        if module:
            where += " AND m.Module = ?"
            extra.append(module)
        by_kind: dict[str, int] = {}
        by_module: dict[str, int] = {}
        rows: list[tuple[Any, ...]] = []
        top, tail = self.sql.top(offset + limit)
        for chunk in self._chunks(ids):
            marks = ",".join("?" * len(chunk))
            joins = f"FROM [References] r JOIN Names o ON o.Id = r.{other} JOIN Modules m ON m.Id = o.ModuleId"
            condition = f"WHERE r.{mine} IN ({marks}){where}"
            for number, module_name, count in self._query(
                    f"SELECT r.Kind, m.Module, COUNT(*) {joins} {condition} GROUP BY r.Kind, m.Module",
                    chunk + extra):
                name = KINDS.get(number, str(number))
                by_kind[name] = by_kind.get(name, 0) + count
                by_module[module_name] = by_module.get(module_name, 0) + count
            rows += self._query(
                f"SELECT {top}o.Path, r.Kind, r.Line, r.[Column], m.Module, t.Path {joins} "
                f"JOIN Names t ON t.Id = r.{mine} {condition} ORDER BY o.Path{self.sql.binary}, r.Line, r.[Column], "
                f"r.Kind, t.Path{self.sql.binary}{tail}", chunk + extra)
        # the same total order as the SQL above (NULL first, like SQL Server), so pages never overlap
        rows.sort(key=lambda r: (r[0], -1 if r[2] is None else r[2], -1 if r[3] is None else r[3], r[1], r[5]))
        total = sum(by_kind.values())
        page = rows[offset:offset + limit]
        key = "source" if direction == "used_by" else "target"
        out_rows = []
        for other_path, number, line, column, module_name, own_path in page:
            row: dict[str, Any] = {key: other_path, "kind": KINDS.get(number, str(number)),
                                   "line": line, "column": column, "module": module_name}
            if members:
                row["of"] = own_path  # which member of `path`
            out_rows.append(row)
        return {"path": path, "direction": direction, "names": len(ids), "total": total,
                "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
                "by_module": dict(sorted(by_module.items(), key=lambda kv: -kv[1])[:20]),
                "rows": out_rows, "offset": offset, "shown": len(out_rows),
                "truncated": offset + len(out_rows) < total, "seconds": round(time.monotonic() - started, 3)}

    def hierarchy(self, name: str, direction: str = "down", limit: int = CLASSES_DEFAULT) -> dict[str, Any]:
        """down: classes extending `name` (and implementing it, for an interface), all levels;
        up: what `name` extends, up to the root, and the interfaces it implements."""
        if direction not in ("down", "up"):
            raise Refused("invalid_request", "direction is down or up", direction=direction)
        _bounded("limit", limit, CLASSES_MAX)
        if not _NAME.fullmatch(name) or name.startswith("@"):
            raise Refused("invalid_request", "a class or interface name", name=name)
        started = time.monotonic()
        own = self._query("SELECT Id, Path FROM Names WHERE Path IN (?, ?)",
                          [f"/Classes/{name}", f"/ClrType/{name}"])
        if not own:
            raise Refused("not_found", "no such class in the cross-reference database", name=name)
        if direction == "down":
            found: dict[str, dict[str, Any]] = {}
            frontier = [(row[0], 0) for row in own]
            seen = {row[0] for row in own}
            deeper = False
            while frontier:
                ids = [i for i, _ in frontier]
                depth = frontier[0][1] + 1
                if depth > HIERARCHY_DEPTH:
                    deeper = True
                    break
                frontier = []
                for chunk in self._chunks(ids):
                    marks = ",".join("?" * len(chunk))
                    # implements and extends at every level: an interface extending an interface has
                    # implementers of its own
                    for source_id, path, kind, module in self._query(
                            "SELECT DISTINCT r.SourceId, s.Path, r.Kind, m.Module FROM [References] r "
                            "JOIN Names s ON s.Id = r.SourceId JOIN Modules m ON m.Id = s.ModuleId "
                            f"WHERE r.TargetId IN ({marks}) AND r.Kind IN (3, 4)", chunk):
                        if source_id in seen:
                            continue
                        seen.add(source_id)
                        found[path] = {"class": _class_name(path), "depth": depth, "via": KINDS[kind],
                                       "module": module}
                        frontier.append((source_id, depth))
            classes = sorted(found.values(), key=lambda c: (c["depth"], c["class"]))
            by_depth: dict[str, int] = {}
            for c in classes:
                by_depth[str(c["depth"])] = by_depth.get(str(c["depth"]), 0) + 1
            return {"name": name, "direction": "down", "total": len(classes), "by_depth": by_depth,
                    "classes": classes[:limit], "shown": min(len(classes), limit),
                    "truncated": len(classes) > limit or deeper, **({"deeper_than": HIERARCHY_DEPTH} if deeper else {}),
                    "seconds": round(time.monotonic() - started, 3)}
        chain = []
        interfaces: list[str] = []
        current = [row[0] for row in own]
        seen = set(current)
        for _ in range(HIERARCHY_DEPTH):
            marks = ",".join("?" * len(current))
            rows = self._query("SELECT DISTINCT r.TargetId, t.Path, r.Kind FROM [References] r "
                               "JOIN Names t ON t.Id = r.TargetId "
                               f"WHERE r.SourceId IN ({marks}) AND r.Kind IN (3, 4)", current)
            rows.sort(key=lambda r: r[1])  # deterministic: /Classes/ before /ClrType/
            interfaces += [_class_name(path) for _, path, kind in rows if kind == 3]
            parents = [(target, path) for target, path, kind in rows if kind == 4 and target not in seen]
            if not parents:
                break
            chain.append(_class_name(parents[0][1]))
            current = [parents[0][0]]
            seen.add(parents[0][0])
        return {"name": name, "direction": "up", "extends": chain, "implements": sorted(set(interfaces)),
                "seconds": round(time.monotonic() - started, 3)}


def _class_name(path: str) -> str:
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) > 1 else path


def _bounded(name: str, value: int, maximum: int) -> None:
    if not 1 <= value <= maximum:
        raise Refused("invalid_request", f"{name} must be 1-{maximum}")


def _first_line(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text.splitlines()[0][:300]
