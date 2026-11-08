"""Read-only D365 log sources and bounded presentation."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, get_args

from boring_service_kit.outcomes import Refused

from . import sqlserver
from .d365 import Session
from .d365_infolog import MAX_INFO_BYTES, decode_info

LogSource = Literal["batch", "process", "events", "iis"]
LogLevel = Literal["error", "warning", "all"]
SOURCES = get_args(LogSource)
FILTER_LEVELS = get_args(LogLevel)
LIMIT_DEFAULT, LIMIT_MAX = 50, 500
CHARS_DEFAULT, CHARS_MAX = 32_768, 262_144
SCAN_ROWS = 1000
INFO_SCAN_BYTES = 32 * 1024 * 1024
MESSAGE_CHARS = 16_384
SINCE_MAX_MINUTES = 30 * 24 * 60
SKIP_MAX = 100_000
LEVELS = ("error", "warning", "info", "unknown")

# D365 10.0.49, ApplicationPlatform/AxEnum/BatchStatus.xml.
BATCH_STATUS = dict(enumerate(("Hold", "Waiting", "Executing", "Error", "Finished",
                               "Ready", "NotRun", "Cancelling", "Canceled", "Scheduled")))
BATCH_STATUS_CODES = {name.casefold(): code for code, name in BATCH_STATUS.items()}
# Kernel enum, Microsoft.Dynamics.AX.Xpp.Support.dll, documented by Microsoft Learn.
EXCEPTION_NAMES = dict(enumerate(("Info", "Warning", "Deadlock", "Error", "Internal", "Break", "DDEerror",
                                  "Sequence", "Numeric", "CLRError", "CodeAccessSecurity", "UpdateConflict",
                                  "UpdateConflictNotRecovered", "DuplicateKeyException",
                                  "DuplicateKeyExceptionNotRecovered", "Timeout", "PassClrObjectAcrossTiers",
                                  "NoValidRunnableCode", "ViewDataSourceValidation")))
EXCEPTION_NAMES.update({21: "SystemMaintenance", 22: "CompanyAccessViolation", 23: "TransientSqlConnectionError",
                        24: "PersistentSqlConnectionError", 61: "MethodNotFound", 79: "TypeCast",
                        83: "FunctionArgument", 91: "ItemNotOpened", 92: "ItemDoesNotExist"})

# Batch tables are system tables: DATAPARTITION (text), not PARTITION (live on a 10.0.49 dev box).
BATCH_SQL = """
WITH recent AS (
    SELECT TOP (?) b.RECID, b.DATAPARTITION, j.CAPTION AS job_caption,
        COALESCE(NULLIF(b.ENDDATETIME, CONVERT(datetime, '19000101', 112)), b.CREATEDDATETIME) AS event_time
    FROM dbo.BATCHHISTORY AS b
    LEFT JOIN dbo.BATCHJOBHISTORY AS j ON j.RECID = b.BATCHJOBHISTORYID AND j.DATAPARTITION = b.DATAPARTITION
    WHERE (((b.ENDDATETIME >= ? AND b.ENDDATETIME < ?)
        OR ((b.ENDDATETIME IS NULL OR b.ENDDATETIME = CONVERT(datetime, '19000101', 112))
            AND b.CREATEDDATETIME >= ? AND b.CREATEDDATETIME < ?))
       -- a continuation (until_skip) also reads the rows at exactly `until`; the caller skips the
       -- ones already returned. datetime2(3) matches the millisecond value the driver handed out.
       OR (? = 1 AND CONVERT(datetime2(3), COALESCE(NULLIF(b.ENDDATETIME, CONVERT(datetime, '19000101', 112)),
                                                   b.CREATEDDATETIME)) = ?))
      -- level error/warning: only failed tasks or tasks with an infolog, so finished
      -- tasks without messages cannot use up the scan (a dev box: ~170 tasks an hour)
      AND (? = 1 OR b.STATUS = 3 OR DATALENGTH(b.INFO) > 0)
      AND (? = 0 OR b.STATUS = ?)
      AND (? = 0 OR b.CAPTION COLLATE Latin1_General_100_CI_AS LIKE ? ESCAPE N'~'
                 OR j.CAPTION COLLATE Latin1_General_100_CI_AS LIKE ? ESCAPE N'~')
    ORDER BY event_time DESC, b.RECID DESC
)
SELECT r.event_time, r.job_caption, b.BATCHJOBID, b.CAPTION, b.BATCHID, b.COMPANY, b.STATUS,
       b.EXECUTEDBY, b.STARTDATETIME, b.ENDDATETIME, DATALENGTH(b.INFO),
       CASE WHEN DATALENGTH(b.INFO) <= ? THEN b.INFO END
FROM recent AS r
JOIN dbo.BATCHHISTORY AS b ON b.RECID = r.RECID AND b.DATAPARTITION = r.DATAPARTITION
ORDER BY r.event_time DESC, r.RECID DESC
"""


PROCESS_SQL = """
SELECT TOP (?) m.CREATEDDATETIME, m.MESSAGE, m.EXCEPTION,
       caption.process_name,
       m.PROCESSEXECUTIONSOURCELINK, l.EXECUTIONID
FROM dbo.PROCESSEXECUTIONMESSAGELOG AS m
LEFT JOIN dbo.PROCESSEXECUTIONSOURCELINK AS l
    ON l.RECID = m.PROCESSEXECUTIONSOURCELINK AND l.PARTITION = m.PARTITION
LEFT JOIN dbo.PROCESSSCHEDULEOCCURRENCE AS o
    ON o.RECID = l.PROCESSSCHEDULEOCCURRENCE AND o.PARTITION = l.PARTITION
LEFT JOIN dbo.PROCESSSCHEDULESERIESPATTERN AS p
    ON p.RECID = l.PROCESSSCHEDULESERIESPATTERN AND p.PARTITION = l.PARTITION
LEFT JOIN dbo.PROCESSSCHEDULESERIES AS s
    ON s.RECID = p.PROCESSSCHEDULESERIES AND s.PARTITION = p.PARTITION
CROSS APPLY (VALUES (COALESCE(NULLIF(o.NAME, ''), NULLIF(s.NAME, ''), NULLIF(l.HEADER, '')))) AS caption(process_name)
WHERE m.CREATEDDATETIME >= ?
  AND (m.CREATEDDATETIME < ? OR ? = 1 AND CONVERT(datetime2(3), m.CREATEDDATETIME) = ?)  -- see BATCH_SQL
  AND (? = 1 OR m.EXCEPTION <> 0)  -- level error/warning: info messages cannot use up the scan
  AND (? = 0 OR caption.process_name COLLATE Latin1_General_100_CI_AS LIKE ? ESCAPE N'~')
ORDER BY m.CREATEDDATETIME DESC, m.RECID DESC
"""


@dataclass
class Entry:
    time: datetime
    level: str
    text: str
    context: dict[str, Any]
    key: tuple[Any, ...]
    undecoded: int | None = None
    elapsed_ms: int | None = None


@dataclass
class Scan:
    """Scan notes and the paging cursor.

    A cut scan continues with the cursor (until=T, until_skip=n): read rows up to and including T,
    in the source's newest-first order, and pass over the first n rows stamped exactly T, which
    earlier pages already scanned. An exclusive `until` alone would lose the rows tied at T that
    did not fit, and many rows share a second (process messages, IIS requests)."""
    notes: list[str] = field(default_factory=list)
    before: datetime | None = None
    before_skip: int = 0
    until: datetime | None = None
    until_skip: int | None = None

    @property
    def inclusive(self) -> bool:
        return self.until_skip is not None

    def in_window(self, stamp: datetime, since: datetime, until: datetime) -> bool:
        return since <= stamp < until or self.inclusive and stamp == until

    def continue_before(self, stamp: datetime | None, seen: int = 0) -> None:
        """`seen`: rows stamped `stamp` that this page scanned; earlier pages' rows are added here."""
        if stamp is None:
            return
        if stamp == self.until and self.until_skip:
            seen += self.until_skip
        if self.before is None or stamp > self.before:
            self.before, self.before_skip = stamp, seen
        elif stamp == self.before:
            # Two cuts at one time (Windows channels): the smaller count may repeat rows, never lose them.
            self.before_skip = min(self.before_skip, seen)


def skip_seen(entries: list[Entry], scan: Scan) -> list[Entry]:
    """Drop the first until_skip entries stamped exactly `until` from a newest-first list."""
    left = scan.until_skip or 0
    kept = []
    for entry in entries:
        if left and entry.time == scan.until:
            left -= 1
            continue
        kept.append(entry)
    return kept


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_since(since: int | str | None, now: datetime, *, name: str = "since") -> datetime:
    now = _utc(now)
    if since is None:
        return now if name == "until" else now - timedelta(hours=1)
    if type(since) is int and 1 <= since <= SINCE_MAX_MINUTES:
        return now - timedelta(minutes=since)
    if isinstance(since, str) and len(since) <= 64:
        try:
            value = _utc(datetime.fromisoformat(since[:-1] + "+00:00" if since.endswith("Z") else since))
        except (ValueError, OverflowError):
            pass
        else:
            if now - timedelta(minutes=SINCE_MAX_MINUTES) <= value <= now:
                return value
    raise Refused("invalid_request", f"{name} must be 1–43,200 minutes or an ISO time within the past 30 days")


_GROUP_TIME = re.compile(
    r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:[.,][0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})?(?![0-9])")
_GROUP_GUID = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f])",
    re.IGNORECASE)
_GROUP_NUMBER = re.compile(r"[0-9]{5,}")
_GROUP_PID = re.compile(r"(\bprocess id(?: was)?[ \t]*[:=]?[ \t]*['\"]?)[0-9]+", re.IGNORECASE)


def normalise_group_text(text: str) -> str:
    """Mask variable identifiers for grouping only."""
    text = _GROUP_GUID.sub("<guid>", _GROUP_TIME.sub("<time>", text))
    text = _GROUP_PID.sub(r"\g<1><n>", text)
    return _GROUP_NUMBER.sub("<n>", text)


def exception_level(kind: int) -> str:
    if kind == 0:
        return "info"
    if kind == 1:
        return "warning"
    return "error" if kind in EXCEPTION_NAMES else "unknown"


def _job_pattern(job: str | None) -> str:
    return "%" + re.sub(r"[~%_\[]", lambda match: "~" + match[0], job or "") + "%"


def _batch(connect: Callable[[], Any], since: datetime, until: datetime, deadline: float, scan: Scan,
           every_level: bool, job_filter: str | None, status_code: int | None) -> Iterator[Entry]:
    start, end = since.replace(tzinfo=None), until.replace(tzinfo=None)
    pattern = _job_pattern(job_filter)
    skip = scan.until_skip or 0
    parameters = (SCAN_ROWS + 1 + skip, start, end, start, end, int(scan.inclusive), end, int(every_level),
                  int(status_code is not None), status_code if status_code is not None else 0,
                  int(job_filter is not None),
                  pattern, pattern, MAX_INFO_BYTES)
    rows = sqlserver.read_rows(connect, BATCH_SQL, parameters, deadline)
    read_bytes = 0
    oldest, tied, index = None, 0, 0
    with closing(rows):
        for row in rows:
            (stamp, job, job_id, task, task_id, company, status, user, started, ended, size, info) = row
            # ORDER BY puts the rows at exactly `until` first; earlier pages returned the first `skip`
            if skip and _utc(stamp) == scan.until:
                skip -= 1
                continue
            if index == SCAN_ROWS:
                scan.notes.append(f"Batch scan stopped after {SCAN_ROWS} tasks. Totals may be incomplete.")
                scan.continue_before(oldest, tied)
                break
            if read_bytes + len(info or b"") > INFO_SCAN_BYTES:
                scan.notes.append("Batch INFO scan reached 32 MiB. Totals may be incomplete.")
                scan.continue_before(oldest, tied)
                break
            index += 1
            tied = tied + 1 if _utc(stamp) == oldest else 1
            oldest = _utc(stamp)
            read_bytes += len(info or b"")
            if size is not None and size > MAX_INFO_BYTES:
                messages, remainder = [], size
            else:
                messages, remainder = decode_info(bytes(info or b""))
            levels = [exception_level(m.kind) for m in messages]
            level = "error" if status == 3 or "error" in levels else (
                "warning" if "warning" in levels else "unknown" if remainder is not None or "unknown" in levels else "info")
            first_error = next((m for m, severity in zip(messages, levels) if severity == "error"), None)
            first = first_error or (messages[0] if messages else None)
            state = BATCH_STATUS.get(status, f"Unknown ({status})")
            text = "\n".join(f"[{severity}] {m.text}" for m, severity in zip(messages, levels))
            context = {"job_caption": job, "job_id": job_id, "task_caption": task, "task_id": task_id,
                       "company": company, "status": state, "executed_by": user,
                       "start": sqlserver.cell(started), "end": sqlserver.cell(ended),
                       "message_id": first.message_id if first else None}
            group_text = first.text if first else state
            yield Entry(oldest, level, text or f"Batch task status: {state}", context,
                        (normalise_group_text(task or ""), normalise_group_text(group_text)), remainder)


def _process(connect: Callable[[], Any], since: datetime, until: datetime, deadline: float, scan: Scan,
             every_level: bool, job_filter: str | None) -> Iterator[Entry]:
    skip = scan.until_skip or 0
    end = until.replace(tzinfo=None)
    parameters = (SCAN_ROWS + 1 + skip, since.replace(tzinfo=None), end, int(scan.inclusive), end,
                  int(every_level), int(job_filter is not None), _job_pattern(job_filter))
    rows = sqlserver.read_rows(connect, PROCESS_SQL, parameters, deadline)
    oldest, tied, index = None, 0, 0
    with closing(rows):
        for stamp, message, kind, process, link_id, execution_id in rows:
            if skip and _utc(stamp) == scan.until:  # see _batch
                skip -= 1
                continue
            if index == SCAN_ROWS:
                scan.notes.append(f"Process scan stopped after {SCAN_ROWS} messages. Totals may be incomplete.")
                scan.continue_before(oldest, tied)
                break
            index += 1
            tied = tied + 1 if _utc(stamp) == oldest else 1
            oldest = _utc(stamp)
            text = message or ""
            context = {"process": process, "source_link_id": link_id, "execution_id": sqlserver.cell(execution_id),
                       "exception": EXCEPTION_NAMES.get(kind, f"Unknown ({kind})")}
            yield Entry(oldest, exception_level(kind), text, context, (normalise_group_text(text),))


def _present(source: str, entries: Iterator[Entry], scan: Scan, level: str, contains: str | None,
             group: bool, limit: int, max_chars: int, redact: Callable[[Any], Any]) -> dict[str, Any]:
    totals = dict.fromkeys(LEVELS, 0)
    groups: dict[bytes, dict[str, Any]] = {}
    texts: dict[bytes, set[bytes]] = {}
    timings: dict[bytes, tuple[int, int, int]] = {}
    rows: list[dict[str, Any]] = []
    needle = contains.casefold() if contains else ""
    shortened = False
    search_fields = {"batch": ("job_caption", "task_caption"), "process": ("process",),
                     "iis": ("example_path",)}.get(source, ())
    for entry in entries:
        # match what the caller may see: a needle against the raw text would confirm a masked secret
        visible = redact(entry.text)
        if needle and needle not in visible.casefold() and not any(
                needle in (entry.context.get(name) or "").casefold() for name in search_fields):
            continue
        totals[entry.level] += 1
        if level != "all" and (entry.level not in ("error", "warning") or level == "error" and entry.level != "error"):
            continue
        stamp = entry.time.isoformat()
        if group:
            key = hashlib.sha256(repr(entry.key).encode("utf-8")).digest()
            if entry.elapsed_ms is not None:
                measured, total, maximum = timings.get(key, (0, 0, 0))
                timings[key] = measured + 1, total + entry.elapsed_ms, max(maximum, entry.elapsed_ms)
            variants = texts.setdefault(key, set())
            raw_text = entry.context.get("example_path", entry.text)
            variants.add(hashlib.sha256(raw_text.encode("utf-8")).digest())
            previous = groups.get(key)
            if previous is not None:
                if source == "batch":
                    status = entry.context["status"]
                    if status != previous["status"] or "status_counts" in previous:
                        counts = previous.setdefault("status_counts", {previous["status"]: previous["count"]})
                        counts[status] = counts.get(status, 0) + 1
                previous["count"] += 1
                previous["distinct"] = len(variants)
                previous["first"] = min(previous["first"], stamp)
                if LEVELS.index(entry.level) < LEVELS.index(previous["level"]):
                    previous["level"] = entry.level
                if entry.undecoded is not None:
                    previous["undecoded"] = previous.get("undecoded", 0) + entry.undecoded
                continue
        text = visible
        shortened |= len(text) > MESSAGE_CHARS
        row = {"source": source, "level": entry.level, "text": text[:MESSAGE_CHARS],
               "context": redact(entry.context)}
        if source == "batch":
            row["status"] = row["context"]["status"]
        if entry.undecoded is not None:
            row["undecoded"] = entry.undecoded
        if group:
            row.update(first=stamp, last=stamp, count=1, distinct=1)
            if source == "iis":
                row["context"].update(max_time_taken=None, avg_time_taken=None)
            groups[key] = row
        else:
            row["time"] = stamp
        rows.append(row)
    for key, (measured, total, maximum) in timings.items():
        groups[key]["context"].update(max_time_taken=maximum, avg_time_taken=round(total / measured, 2))
    shown: list[dict[str, Any]] = []
    size = 2
    for row in rows[:limit]:
        length = len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) + bool(shown)
        if size + length > max_chars:
            break
        shown.append(row)
        size += length
    notes = list(scan.notes)
    if scan.before is not None:
        stamp = scan.before.isoformat()
        notes.append(f"Oldest time reached for continuation: {stamp}. Continue with until={stamp} and "
                     f"until_skip={scan.before_skip} and the same other filters, for the rows not yet scanned.")
    if shortened:
        notes.append(f"Some messages were cut at {MESSAGE_CHARS:,} characters.")
    if len(shown) < len(rows):
        notes.append(f"Only the first {len(shown)} of {len(rows)} rows fit. Raise limit or max_chars, or narrow the window (since/until) or the filters applied in the scan (level, job, status).")
    result = {"source": source, "available": True, "rows": shown, "shown": len(shown),
              "totals": totals, "truncated": bool(notes)}
    if notes:
        result["note"] = " ".join(notes)
    return result


def read_logs(source: str, since: int | str | None, level: str, contains: str | None, group: bool,
              limit: int, max_chars: int, sql_connect: Callable[..., Any], timeout: float,
              redact: Callable[[Any], Any], session: Session, *, until: int | str | None = None,
              job: str | None = None, status: str | None = None, until_skip: int | None = None) -> dict[str, Any]:
    if source not in SOURCES:
        raise Refused("invalid_request", f"source must be one of: {', '.join(SOURCES)}")
    if level not in FILTER_LEVELS:
        raise Refused("invalid_request", f"level must be one of: {', '.join(FILTER_LEVELS)}")
    status_code = None
    if status is not None:
        if source != "batch":
            raise Refused("invalid_request", "status is available only for batch")
        status_code = BATCH_STATUS_CODES.get(status.casefold())
        if status_code is None:
            raise Refused("invalid_request", f"status must be one of: {', '.join(BATCH_STATUS_CODES)}")
    if job is not None:
        if source not in ("batch", "process"):
            raise Refused("invalid_request", "job is available only for batch and process")
        if len(job) > 200:
            raise Refused("invalid_request", "job must be at most 200 characters")
    if until_skip is not None:
        # a minute offset moves between calls, so the cursor needs the fixed ISO time a note gave
        if type(until_skip) is not int or not 0 <= until_skip <= SKIP_MAX:
            raise Refused("invalid_request", f"until_skip must be 0–{SKIP_MAX:,}")
        if not isinstance(until, str):
            raise Refused("invalid_request", "until_skip requires an ISO until, as given by a continuation note")
    if contains is not None and len(contains) > 1000:
        raise Refused("invalid_request", "contains must be at most 1,000 characters")
    if type(limit) is not int or not 1 <= limit <= LIMIT_MAX:
        raise Refused("invalid_request", f"limit must be 1–{LIMIT_MAX}")
    if type(max_chars) is not int or not 1 <= max_chars <= CHARS_MAX:
        raise Refused("invalid_request", f"max_chars must be 1–{CHARS_MAX}")
    now = datetime.now(timezone.utc)
    start = parse_since(since, now)
    end = parse_since(until, now, name="until")
    if end <= start:
        raise Refused("invalid_request", "until must be later than since")
    scan = Scan(until=end, until_skip=until_skip)
    deadline = time.monotonic() + timeout
    if source in {"events", "iis"}:
        from . import d365_box_logs
        try:
            if source == "events":
                entries = d365_box_logs.events(session, start, end, timeout, scan, level)
            else:
                entries = d365_box_logs.iis(start, end, deadline, scan, group)
        except d365_box_logs.Unavailable as error:
            return {"available": False, "reason": str(error)}
        return _present(source, iter(entries), scan, level, contains, group, limit, max_chars, redact)
    connect = lambda: sql_connect("AxDB", timeout)()
    if level != "all":
        scan.notes.append(f"{source} was read at the requested level only; totals for lower levels are not counted.")
    if source == "batch":
        read = _batch(connect, start, end, deadline, scan, level == "all", job, status_code)
    else:
        read = _process(connect, start, end, deadline, scan, level == "all", job)
    with closing(read) as entries:
        return _present(source, entries, scan, level, contains, group, limit, max_chars, redact)
