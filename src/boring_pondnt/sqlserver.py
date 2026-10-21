"""SQL Server on the box, shared by the xref tools (read-only queries) and boring-pondnt-d365
(`d365_sql`): an ODBC connection with Windows authentication, and a small executor that turns a
script into JSON -- result sets, rows changed, messages -- with the traps of sqlcmd taken out
(exit code 0 on errors and timeouts, code pages, text output cut in the middle).
"""

from __future__ import annotations

import datetime
import decimal
import math
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from boring_service_kit.outcomes import Failed, Refused

ROWS_DEFAULT, ROWS_MAX = 100, 1000
CELL_CHARS = 2000
RESULT_SETS_MAX = 20
TIMEOUT_DEFAULT, TIMEOUT_MAX = 60, 3600
_GO = re.compile(r"^[ \t]*GO[ \t]*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
DATABASE = re.compile(r"[A-Za-z0-9_\-]{1,128}")


def odbc_connector(server: str, database: str, driver: str | None = None, timeout: float = 120.0,
                   app: str = "boring-pondnt", autocommit: bool = True,
                   read_uncommitted: bool = False) -> Callable[[], Any]:
    """A function that opens one connection with Windows authentication; no password anywhere."""
    import pyodbc  # noqa: PLC0415 -- Windows only (pyproject marker)

    if driver is None:
        installed = pyodbc.drivers()
        modern = sorted((d for d in installed if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
                        key=lambda d: int(re.findall(r"\d+", d)[0]))
        driver = modern[-1] if modern else "SQL Server"  # the old one ships with Windows
    text = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
            f"TrustServerCertificate=yes;APP={app}")

    def connect() -> Any:
        connection = pyodbc.connect(text, autocommit=autocommit, timeout=15)
        connection.timeout = max(1, math.ceil(timeout)) if timeout else 0  # per query; 0 = never
        if read_uncommitted:
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        return connection

    connect.description = f"{driver}; {server}; {database}"  # type: ignore[attr-defined]
    return connect


def batches(script: str) -> list[str]:
    """Split a script at `GO` lines, as sqlcmd and SQL Server Management Studio do (ODBC does not)."""
    return [part.strip() for part in _GO.split(script) if part.strip()]


def cell(value: Any) -> Any:
    """A value as JSON: exact decimals as text, times in ISO form, binary as hex, long text cut."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return format(value.normalize(), "f")  # numeric(32,16) of D365: 1.5000000000000000 -> "1.5"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = "0x" + raw[:CELL_CHARS // 2].hex().upper()
        return text + ("..." if len(raw) > CELL_CHARS // 2 else "")
    text = str(value)
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + f"... ({len(text)} chars)"


def clean(text: str) -> str:
    """SQL Server's own words: pyodbc wraps them in the state, the driver's names and the API call --
    ('42S02', "[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object name 'X'. (208) (SQLExecDirectW)")."""
    text = re.sub(r"^\('[0-9A-Z]{5}', [\"']", "", text.strip())
    parts = []
    for part in re.split(r";\s*(?=\[[0-9A-Z]{5}\])", text):  # several diagnostic records in one error
        part = re.sub(r"^(\s*\[[^\]]*\])+\s*", "", part)
        part = re.sub(r"\s*\(\d+\)\s*(\(SQL\w+\))?[\"')]*\s*$", "", part)
        parts.append(part.strip())
    return " ".join(p for p in parts if p)


def _first_line(error: BaseException) -> str:
    text = clean(str(error)) or type(error).__name__
    return text.splitlines()[0][:500]


def execute(connect: Callable[[], Any], script: str, max_rows: int = ROWS_DEFAULT,
            atomic: bool = False, deadline: float | None = None) -> dict[str, Any]:
    """Run a script batch by batch. Each result set comes back as columns and up to `max_rows` rows;
    statements without one as rows changed. `atomic`: one transaction -- any error rolls all back.
    An error is a Failed outcome naming the batch and what had already happened.

    `deadline` (time.monotonic) bounds the whole script, connecting included: no batch
    starts after it. Passed before the first batch: Refused `not_admitted`, nothing ran.
    Passed later: Failed `timeout` naming the batches that ran (atomic: rolled back).
    A batch already running keeps its own per-query timeout (the connector's)."""
    if not 1 <= max_rows <= ROWS_MAX:
        raise Refused("invalid_request", f"max_rows must be 1-{ROWS_MAX}")
    parts = batches(script)
    if not parts:
        raise Refused("invalid_request", "the script is empty")
    started = time.monotonic()
    try:
        connection = connect()
    except Exception as error:  # noqa: BLE001 -- the driver's own exception types
        raise Failed("unavailable", f"cannot connect: {_first_line(error)}") from None
    results: list[dict[str, Any]] = []
    messages: list[str] = []
    done = 0
    try:
        if atomic:
            try:
                connection.autocommit = False  # pyodbc; sqlite3 (the tests) opens a transaction itself
            except AttributeError:
                pass
        cursor = connection.cursor()
        for number, batch in enumerate(parts, start=1):
            if deadline is not None and time.monotonic() >= deadline:  # last point before it runs
                if atomic:
                    connection.rollback()
                if done == 0:
                    raise Refused("not_admitted", "the script's timeout passed before its first batch; "
                                  "nothing ran", batches=len(parts))
                raise Failed("timeout", f"the script's timeout passed before batch {number}; "
                             f"batches 1-{done} ran, the rest was not started",
                             batch=number, batches=len(parts), batches_done=done,
                             rolled_back=atomic, results=results[-5:])
            try:
                cursor.execute(batch)
                while True:
                    if cursor.description is not None:
                        if len(results) < RESULT_SETS_MAX:
                            columns = [column[0] for column in cursor.description]
                            fetched = cursor.fetchmany(max_rows + 1)
                            rows = [[cell(value) for value in row] for row in fetched[:max_rows]]
                            results.append({"batch": number, "columns": columns, "rows": rows,
                                            "shown": len(rows), "truncated": len(fetched) > max_rows})
                    elif cursor.rowcount is not None and cursor.rowcount >= 0 and len(results) < RESULT_SETS_MAX:
                        results.append({"batch": number, "rows_changed": cursor.rowcount})
                    messages += [clean(str(m[1])) for m in getattr(cursor, "messages", None) or []]
                    nextset = getattr(cursor, "nextset", None)
                    if nextset is None or not nextset():
                        break
                if not atomic and getattr(connection, "autocommit", False) is not True:
                    connection.commit()  # each batch stands on its own, as in sqlcmd
                    # (`is not True`: sqlite3 from Python 3.12 has autocommit = -1, "legacy", truthy)
                done = number
            except Exception as error:  # noqa: BLE001
                message = _first_line(error)
                if atomic:
                    connection.rollback()
                timed_out = "HYT00" in str(error) or "timeout" in message.lower()
                raise Failed("timeout" if timed_out else "sql_error",
                             ("the query ran past its timeout and was stopped" if timed_out else message),
                             batch=number, batches=len(parts), batches_done=done,
                             rolled_back=atomic, results=results[-5:]) from None
        if atomic:
            connection.commit()
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    result: dict[str, Any] = {"batches": len(parts), "atomic": atomic, "results": results,
                              "seconds": round(time.monotonic() - started, 3)}
    if messages:
        result["messages"] = messages[:50]
    if len(results) >= RESULT_SETS_MAX:
        result["note"] = f"only the first {RESULT_SETS_MAX} results are shown"
    return result
