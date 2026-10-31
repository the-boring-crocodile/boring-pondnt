"""boring-pondnt-xref against a SQLite copy of DYNAMICSXREFDB's schema, with names shaped like the
real ones: code paths (/Classes/...), metadata paths (Table/...?Property), labels."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from boring_pondnt import xref
from boring_service_kit.outcomes import Failed, Refused
from boring_pondnt.d365_server import create_server

SCHEMA = """
CREATE TABLE DBVersion (Version TEXT);
CREATE TABLE Providers (Id INTEGER PRIMARY KEY, Provider TEXT);
CREATE TABLE Modules (Id INTEGER PRIMARY KEY, Module TEXT);
CREATE TABLE Names (Id INTEGER PRIMARY KEY, Path TEXT, ProviderId INTEGER, ModuleId INTEGER);
CREATE TABLE [References] (SourceId INTEGER, TargetId INTEGER, Kind INTEGER, Line INTEGER, [Column] INTEGER);
CREATE INDEX Names_Path ON Names (Path);
"""

NAMES = {  # path: (provider, module)
    "/Classes/ContosoBase": (1, 1),
    "/Classes/ContosoBase/Methods/run": (1, 1),
    "/Classes/ContosoBase/Methods/construct": (1, 1),
    "/Classes/ContosoChild": (1, 1),
    "/Classes/ContosoChild/Methods/run": (1, 1),
    "/Classes/ContosoGrandChild": (1, 2),
    "/Classes/ContosoIRunnable": (1, 1),
    "/Classes/ContosoImpl": (1, 2),
    "/Classes/ContosoImplChild": (1, 2),
    "/Classes/Contoso_Util": (1, 1),
    "/Classes/ContosoXUtil": (1, 1),
    "/Tables/ContosoTable": (1, 1),
    "/Tables/ContosoTable/Fields/AccountNum": (1, 1),
    "Table/ContosoTable": (2, 1),
    "Table/ContosoLine/TableRelation/ContosoTable?RelatedTable": (2, 2),
    "Table/ContosoTable/Field/AccountNum?Label": (2, 1),
    "/Labels/@SYS7149": (1, 1),
    "/Edts/Counter": (1, 1),
}
REFS = [  # source, target, kind, line, column
    ("/Classes/ContosoChild", "/Classes/ContosoBase", 4, None, None),
    ("/Classes/ContosoGrandChild", "/Classes/ContosoChild", 4, None, None),
    ("/Classes/ContosoImpl", "/Classes/ContosoIRunnable", 3, None, None),
    ("/Classes/ContosoImplChild", "/Classes/ContosoImpl", 4, None, None),
    ("/Classes/ContosoBase", "/Classes/ContosoIRunnable", 3, None, None),
    ("/Classes/ContosoChild/Methods/run", "/Classes/ContosoBase/Methods/run", 10, 12, 5),
    ("/Classes/ContosoChild/Methods/run", "/Classes/ContosoBase/Methods/construct", 1, 14, 9),
    ("/Classes/ContosoChild/Methods/run", "/Classes/ContosoBase/Methods/construct", 1, 20, 9),
    ("/Classes/ContosoChild/Methods/run", "/Tables/ContosoTable", 2, 13, 9),
    ("/Classes/ContosoChild/Methods/run", "/Edts/Counter", 2, 15, 9),
    ("/Classes/ContosoGrandChild", "/Tables/ContosoTable/Fields/AccountNum", 2, 30, 17),
    ("Table/ContosoLine/TableRelation/ContosoTable?RelatedTable", "/Tables/ContosoTable", 6, None, None),
    ("Table/ContosoTable/Field/AccountNum?Label", "/Labels/@SYS7149", 6, None, None),
    ("/Classes/ContosoImpl", "/Labels/@SYS7149", 1, 40, 21),
]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "xref.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO DBVersion VALUES ('1.0.0.10')")
    connection.executemany("INSERT INTO Providers VALUES (?, ?)", [(1, "Xppc.exe"), (2, "Metadata")])
    connection.executemany("INSERT INTO Modules VALUES (?, ?)", [(1, "ContosoCore"), (2, "ContosoExt")])
    ids = {}
    for number, (name, (provider, module)) in enumerate(NAMES.items(), start=10):
        ids[name] = number
        connection.execute("INSERT INTO Names VALUES (?, ?, ?, ?)", (number, name, provider, module))
    connection.executemany("INSERT INTO [References] VALUES (?, ?, ?, ?, ?)",
                           [(ids[s], ids[t], k, line, col) for s, t, k, line, col in REFS])
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def index(db):
    return xref.Xref(lambda: sqlite3.connect(db, check_same_thread=False), dialect="sqlite")


def test_info_counts_and_kinds(index):
    info = index.info()
    assert info["version"] == "1.0.0.10" and info["providers"] == ["Xppc.exe", "Metadata"]
    assert info["names"] == len(NAMES) and info["references"] == len(REFS) and info["modules"] == 2
    assert info["kinds"][:4] == ["call", "type", "implements", "extends"]


def test_names_by_element_name_label_and_prefix(index):
    found = [n["path"] for n in index.names("ContosoTable")["names"]]
    assert found == ["/Tables/ContosoTable", "Table/ContosoTable"]
    assert index.names("ContosoTable", element_type="Tables")["names"] == [
        {"path": "/Tables/ContosoTable", "module": "ContosoCore"}]
    assert index.names("@SYS7149")["names"][0]["path"] == "/Labels/@SYS7149"
    prefix = index.names("/Classes/ContosoBase*")
    assert [n["path"] for n in prefix["names"]] == [
        "/Classes/ContosoBase", "/Classes/ContosoBase/Methods/construct", "/Classes/ContosoBase/Methods/run"]
    # '_' is a LIKE wildcard: escaped, it matches only itself
    assert [n["path"] for n in index.names("/Classes/Contoso_*")["names"]] == ["/Classes/Contoso_Util"]
    assert index.names("/Classes/Contoso*", limit=2)["truncated"] is True
    nothing = index.names("NoSuchThing")
    assert nothing["names"] == [] and "prefix" in nothing["hint"]
    for bad in ("x; DROP TABLE Names", "/Classes/*Base", "/Classes/A%"):
        with pytest.raises(Refused):
            index.names(bad)
    with pytest.raises(Refused):
        index.names("X", element_type="Gadgets")


def test_used_by_totals_rows_and_filters(index):
    result = index.references("/Classes/ContosoBase/Methods/construct")
    assert result["total"] == 2 and result["by_kind"] == {"call": 2}
    assert [(r["source"], r["line"]) for r in result["rows"]] == [
        ("/Classes/ContosoChild/Methods/run", 14), ("/Classes/ContosoChild/Methods/run", 20)]
    table = index.references("/Tables/ContosoTable")
    assert table["by_kind"] == {"type": 1, "property": 1}
    assert table["by_module"] == {"ContosoCore": 1, "ContosoExt": 1}
    with_members = index.references("/Tables/ContosoTable", members=True)
    assert with_members["names"] == 2 and with_members["total"] == 3
    assert {r["of"] for r in with_members["rows"]} == {"/Tables/ContosoTable", "/Tables/ContosoTable/Fields/AccountNum"}
    assert index.references("/Tables/ContosoTable", kind="property")["total"] == 1
    assert index.references("/Tables/ContosoTable", module="ContosoExt")["rows"][0]["source"].startswith("Table/")
    page = index.references("/Classes/ContosoBase/Methods/construct", limit=1, offset=1)
    assert page["shown"] == 1 and page["rows"][0]["line"] == 20 and page["truncated"] is False
    assert index.references("/Classes/ContosoBase/Methods/construct", limit=1)["truncated"] is True


def test_labels_by_id_in_code_and_metadata(index):
    result = index.references("@SYS7149")
    assert result["path"] == "/Labels/@SYS7149" and result["by_kind"] == {"call": 1, "property": 1}
    assert [r["source"] for r in result["rows"]] == ["/Classes/ContosoImpl", "Table/ContosoTable/Field/AccountNum?Label"]


def test_uses_is_the_other_direction(index):
    result = index.references("/Classes/ContosoChild/Methods/run", direction="uses")
    assert result["total"] == 5 and result["by_kind"] == {"call": 2, "type": 2, "override": 1}
    assert result["rows"][0]["target"] == "/Classes/ContosoBase/Methods/construct"


def test_many_names_are_queried_in_chunks(index, monkeypatch):
    monkeypatch.setattr(xref, "IDS_PER_QUERY", 1)
    result = index.references("/Tables/ContosoTable", members=True)
    assert result["total"] == 3 and result["by_kind"] == {"type": 2, "property": 1}
    sources = [r["source"] for r in result["rows"]]
    assert len(sources) == 3 and sources == sorted(sources)  # merged across chunks, in order


def test_reference_refusals(index):
    with pytest.raises(Refused) as missing:
        index.references("/Classes/Nope")
    assert missing.value.code == "not_found" and "what=names" in missing.value.message
    for kwargs in ({"kind": "reads"}, {"direction": "sideways"}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(Refused):
            index.references("/Classes/ContosoBase", **kwargs)


def test_hierarchy_down_follows_interfaces_and_levels(index):
    down = index.hierarchy("ContosoIRunnable")
    assert [(c["class"], c["depth"], c["via"]) for c in down["classes"]] == [
        ("ContosoBase", 1, "implements"), ("ContosoImpl", 1, "implements"),
        ("ContosoChild", 2, "extends"), ("ContosoImplChild", 2, "extends"), ("ContosoGrandChild", 3, "extends")]
    assert index.hierarchy("ContosoBase")["total"] == 2
    cut = index.hierarchy("ContosoIRunnable", limit=2)
    assert cut["total"] == 5 and cut["shown"] == 2 and cut["truncated"] is True
    assert cut["by_depth"] == {"1": 2, "2": 2, "3": 1}


def test_hierarchy_up_is_the_base_chain(index):
    up = index.hierarchy("ContosoGrandChild", direction="up")
    assert up["extends"] == ["ContosoChild", "ContosoBase"] and up["implements"] == ["ContosoIRunnable"]
    with pytest.raises(Refused):
        index.hierarchy("Nope")


def test_connection_and_query_failures_are_outcomes(db):
    def broken():
        raise RuntimeError("[08001] cannot reach the server\nmore detail")

    with pytest.raises(Failed) as failure:
        xref.Xref(broken, dialect="sqlite").info()
    assert failure.value.code == "unavailable" and "more detail" not in failure.value.message
    empty = xref.Xref(lambda: sqlite3.connect(":memory:"), dialect="sqlite")
    with pytest.raises(Failed) as failure:
        empty.info()
    assert failure.value.code == "query_failed"


def call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    content = blocks.content if hasattr(blocks, "content") else blocks
    return getattr(blocks, "is_error", False), json.loads(content[0].text)


def test_server_has_four_small_read_only_tools(index, tmp_path):
    server = create_server(state_dir=tmp_path, start_session=False, xref_index=index)
    tools = [t for t in asyncio.run(server.list_tools()) if t.name.startswith("xref")]
    assert [t.name for t in tools] == ["xref"]  # D40: one tool for the four lookups
    assert all(len(t.description or "") <= 120 and t.annotations.read_only_hint for t in tools)
    is_error, data = call(server, "xref", {"what": "references", "path": "@SYS7149", "direction": "used_by"})
    assert not is_error and data["total"] == 2
    with pytest.raises(ToolError, match="unknown: sql"):
        call(server, "xref", {"what": "references", "path": "@SYS7149", "sql": "DROP"})
    is_error, data = call(server, "xref", {"what": "names", "name": "/Classes/*Base"})
    assert is_error and data["code"] == "invalid_request"


def test_one_xref_tool_dispatches_and_names_what_each_lookup_needs(index, tmp_path):
    server = create_server(state_dir=tmp_path, start_session=False, xref_index=index)
    is_error, data = call(server, "xref", {"what": "info"})
    assert not is_error and "names" in json.dumps(data)
    is_error, data = call(server, "xref", {"what": "names", "name": "ContosoBase"})
    assert not is_error and data["names"]
    is_error, data = call(server, "xref", {"what": "hierarchy", "name": "ContosoBase"})
    assert not is_error and data["direction"] == "down"
    for args, needs in (({"what": "names"}, "needs name"), ({"what": "references", "name": "x"}, "not name"),
                        ({"what": "info", "limit": 5}, "not limit"), ({"what": "hierarchy", "path": "/x"}, "needs name"),
                        ({"what": "everything"}, "info, names, references, hierarchy")):
        is_error, data = call(server, "xref", args)
        assert is_error and data["code"] == "invalid_request" and needs in data["message"], (args, data)


def test_pages_neither_skip_nor_repeat_under_a_case_insensitive_collation(tmp_path):
    """SQL Server compares paths ignoring case: SQL and Python must agree on the order of a page."""
    path = tmp_path / "ci.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA.replace("Path TEXT", "Path TEXT COLLATE NOCASE"))
    connection.execute("INSERT INTO Modules VALUES (1, 'M')")
    names = ["/Classes/T", "/Classes/a", "/Classes/B", "/Classes/c", "/Classes/_x", "/Classes/D-y"]
    for number, name in enumerate(names, start=1):
        connection.execute("INSERT INTO Names VALUES (?, ?, 1, 1)", (number, name))
    connection.executemany("INSERT INTO [References] VALUES (?, 1, 1, 7, 3)", [(n,) for n in range(2, 7)])
    connection.commit()
    connection.close()
    index = xref.Xref(lambda: sqlite3.connect(path, check_same_thread=False), dialect="sqlite")
    seen = [index.references("/Classes/T", limit=1, offset=k)["rows"][0]["source"] for k in range(5)]
    assert sorted(seen) == sorted(names[1:]) and len(set(seen)) == 5


def test_hierarchy_finds_implementers_of_sub_interfaces(db):
    connection = sqlite3.connect(db)
    connection.executemany("INSERT INTO Names VALUES (?, ?, 1, 1)",
                           [(900, "/Classes/ContosoISub"), (901, "/Classes/ContosoSubImpl")])
    base = connection.execute("SELECT Id FROM Names WHERE Path = '/Classes/ContosoIRunnable'").fetchone()[0]
    connection.executemany("INSERT INTO [References] VALUES (?, ?, ?, NULL, NULL)", [(900, base, 4), (901, 900, 3)])
    connection.commit()
    connection.close()
    index = xref.Xref(lambda: sqlite3.connect(db, check_same_thread=False), dialect="sqlite")
    down = {c["class"]: (c["depth"], c["via"]) for c in index.hierarchy("ContosoIRunnable", limit=50)["classes"]}
    assert down["ContosoISub"] == (1, "extends") and down["ContosoSubImpl"] == (2, "implements")


def test_members_include_metadata_properties(index):
    result = index.references("Table/ContosoTable/Field/AccountNum", direction="uses", members=True)
    assert result["names"] == 1 and result["rows"][0]["of"] == "Table/ContosoTable/Field/AccountNum?Label"


def test_deep_paging_is_refused(index):
    with pytest.raises(Refused) as refused:
        index.references("/Tables/ContosoTable", limit=500, offset=9_600)
    assert "narrow" in refused.value.message


class SlowCursor:
    """Records each query sent; each takes `seconds`."""

    def __init__(self, cursor, sent: list[str], seconds: float) -> None:
        self.cursor, self.sent, self.seconds = cursor, sent, seconds

    def execute(self, sql, params):
        import time

        self.sent.append(sql)
        time.sleep(self.seconds)
        return self.cursor.execute(sql, params)

    def fetchall(self):
        return self.cursor.fetchall()


def test_no_xref_query_starts_after_the_calls_budget(db, tmp_path):
    """A multi-query tool has one deadline behind a Pond (the xref path)."""
    sent: list[str] = []
    connection = sqlite3.connect(db, check_same_thread=False)
    slow = type("SlowConnection", (), {"cursor": lambda self: SlowCursor(connection.cursor(), sent, 0.4),
                                       "close": lambda self: None})()
    index = xref.Xref(lambda: slow, dialect="sqlite")
    server = create_server(state_dir=tmp_path, start_session=False, xref_index=index, call_budget=0.6)
    try:
        # used_by needs three queries (names, totals, rows): the third would start at ~0.8 s.
        is_error, data = call(server, "xref", {"what": "references", "path": "/Classes/ContosoBase"})
        assert is_error and data["code"] == "timeout" and "before its next query" in data["message"]
        assert len(sent) == 2
        sent.clear()
        with index.deadline(None):  # without a budget (loopback), every query runs
            assert index.references("/Classes/ContosoBase")["total"] >= 1 and len(sent) == 3
    finally:
        server._d365_close()
