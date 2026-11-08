"""Classic-box D365 logs. Only fixed OS log sources are read."""
from __future__ import annotations

import os
import re
import stat
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from boring_service_kit.outcomes import Failed

from .d365 import Session, ps_quote
from .d365_logs import Entry, Scan, _utc, normalise_group_text, skip_seen


class Unavailable(Exception):
    """The requested OS log source is absent."""


_D365_FAULT = re.compile(
    r"\b(?:AOSService|Batch\.exe|w3wp(?:\.exe)?)\b|"
    r"(?:^|[\\/])PackagesLocalDirectory[\\/]|"
    r"(?:^|[\\/])Dynamics\.AX\.[^\\/\s]+\.dll\b", re.IGNORECASE)


# Filter by channel and time before materializing events. Provider and message filters are
# alternatives, so an unrelated provider mentioning the AOS can still be included.
_EVENTS = r"""
function Format-LogTime($stamp) {
    $stamp.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ', [Globalization.CultureInfo]::InvariantCulture)
}
$channels = @('Application', 'System')
$events = [System.Collections.Generic.List[object]]::new()
$present = 0
$scanCut = $false
$cutoffs = [System.Collections.Generic.List[string]]::new()
foreach ($channel in $channels) {
    try {
        $null = Get-WinEvent -ListLog $channel -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatchingLogsFound,*') { continue }
        throw
    }
    $present++
    try {
        # The window in UTC (XPath on SystemTime): a local-time window shifts by an hour in the
        # repeated hour of the daylight-saving change. Levels in the query, so info noise does not
        # use up the 1,000-record scan.
        $system = "TimeCreated[@SystemTime>='$since' and @SystemTime<='$until']"
        if ($levels) { $system += ' and (' + (($levels | ForEach-Object { "Level=$_" }) -join ' or ') + ')' }
        $records = @(Get-WinEvent -LogName $channel -FilterXPath "*[System[$system]]" -MaxEvents 1001 -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound,*') { continue }
        throw
    }
    if ($records.Count -gt 1000) {
        $scanCut = $true
        $cutoffs.Add((Format-LogTime $records[999].TimeCreated))
    }
    foreach ($event in ($records | Select-Object -First 1000)) { $events.Add($event) }
}
$rows = [System.Collections.Generic.List[object]]::new()
$size = 0
$textCut = $false
$previous = $null
foreach ($event in ($events | Sort-Object { $_.TimeCreated.ToUniversalTime() } -Descending)) {  # UTC: the DST hour repeats local times
    $provider = [string]$event.ProviderName
    $message = [string]$event.Message
    $textSource = $null
    if ([string]::IsNullOrWhiteSpace($message)) {
        $insertions = foreach ($property in $event.Properties) {
            $value = [string]$property.Value
            if (-not [string]::IsNullOrWhiteSpace($value)) { $value }
        }
        $message = $insertions -join ([Environment]::NewLine)
        if ($message) {
            $textSource = 'insertion_strings'
        } else {
            $message = 'no message text and no insertion strings'
            $textSource = 'unavailable'
        }
    }
    $faulting = @()
    if ($provider -eq 'Application Error') {
        $faulting = @(([xml]$event.ToXml()).Event.EventData.Data |
            Where-Object { $_.Name -in @('AppName', 'ModuleName', 'AppPath', 'ModulePath') } |
            ForEach-Object { $_.InnerText })
        if (-not @($faulting | Where-Object { $_ -match $d365Fault }).Count) {
            $previous = $event.TimeCreated
            continue
        }
    }
    $name = [regex]::Match($message, '\b(AOSService|Batch\.exe|w3wp(?:\.exe)?)\b', 'IgnoreCase')
    if ($provider -notin @('WAS', 'Application Error', '.NET Runtime', 'IIS-W3SVC-WP',
                              'Microsoft-Windows-WAS', 'Microsoft-Windows-IIS-W3SVC-WP') -and
        $provider -notlike 'Management Reporter*' -and $provider -notlike 'Dynamics*' -and
        -not $name.Success) {
        $previous = $event.TimeCreated
        continue
    }
    # Reserve enough room for the other fields and JSON escaping. Omit an oversized
    # message whole, so a credential cannot be cut before the server masks it.
    $cost = $message.Length + $provider.Length + 160
    if ($size + $cost -gt 100000) {
        $textCut = $true
        if ($cost -gt 100000) { continue }
        if ($null -ne $previous) { $cutoffs.Add((Format-LogTime $previous)) }
        break
    }
    $size += $cost
    $previous = $event.TimeCreated
    $rows.Add(@{
        time=(Format-LogTime $event.TimeCreated)
        level=[int]$event.Level; log=[string]$event.LogName; provider=$provider; event_id=[int]$event.Id
        process=$event.ProcessId; process_name=$name.Value; message=$message; text_source=$textSource; faulting=$faulting
    })
}
@{ available=($present -gt 0); scan_cut=$scanCut; text_cut=$textCut; rows=$rows.ToArray(); cutoffs=$cutoffs.ToArray() }
"""


EVENT_LEVELS = {"error": "@(1, 2)", "warning": "@(1, 2, 3)", "all": "$null"}


def _event_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value))


def events(session: Session, since: datetime, until: datetime, timeout: float, scan: Scan,
           level: str = "all") -> list[Entry]:
    # EndTime is inclusive and TimeCreated has 100 ns ticks while rows carry microseconds: for a
    # continuation that includes `until`, ask up to the next microsecond and filter here.
    end = until + timedelta(microseconds=1) if scan.inclusive else until
    stamp = lambda t: _utc(t).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")  # noqa: E731 -- SystemTime's form, 100 ns
    script = ("& {\n$since = " + ps_quote(stamp(since)) + "\n"
              + "$until = " + ps_quote(stamp(end)) + "\n"
              + "$levels = " + EVENT_LEVELS[level] + "\n"
              + "$d365Fault = " + ps_quote(_D365_FAULT.pattern) + "\n" + _EVENTS + "\n}")
    reply = session.call(script, timeout=timeout)
    if not isinstance(reply, dict) or type(reply.get("available")) is not bool:
        raise Failed("bad_reply", "invalid Windows event log reply")
    if level != "all" and reply["available"]:
        scan.notes.append("Windows events were read at the requested level only; totals for lower levels are not counted.")
    if not reply["available"]:
        raise Unavailable("Windows Application and System logs are absent")
    if reply.get("scan_cut"):
        scan.notes.append("Windows event scan reached 1,000 records per channel. Totals may be incomplete.")
    if reply.get("text_cut"):
        scan.notes.append("Windows event output reached its text budget. Totals may be incomplete.")
    rows, cutoffs = reply.get("rows"), reply.get("cutoffs", [])
    if not isinstance(rows, list) or not isinstance(cutoffs, list):
        raise Failed("bad_reply", "invalid Windows event log rows")
    entries = []
    try:
        cut = None
        for value in cutoffs:
            stamp = _event_time(value)
            if scan.in_window(stamp, since, until) and (cut is None or stamp > cut):
                cut = stamp
        for row in rows:
            stamp = _event_time(row["time"])
            provider, event_id, text = row["provider"], row["event_id"], row["message"]
            if row["log"] not in {"Application", "System"} or type(event_id) is not int or not isinstance(text, str):
                raise ValueError
            if not scan.in_window(stamp, since, until):
                continue
            if provider.casefold() == "application error":
                faulting = row.get("faulting", [])
                if not isinstance(faulting, list) or not all(isinstance(value, str) for value in faulting):
                    raise ValueError
                if not any(_D365_FAULT.search(value) for value in faulting):
                    continue
            level = {1: "error", 2: "error", 3: "warning", 4: "info", 5: "info"}.get(row["level"], "unknown")
            context = {name: row[name] for name in ("log", "provider", "event_id", "process", "process_name")}
            if row.get("text_source") in ("insertion_strings", "unavailable"):
                context["text_source"] = row["text_source"]
            first_line = normalise_group_text(text.partition("\n")[0].rstrip("\r"))
            entries.append(Entry(stamp, level, text, context, (provider, event_id, first_line)))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise Failed("bad_reply", "invalid Windows event log row") from None
    entries = skip_seen(sorted(entries, key=lambda entry: entry.time, reverse=True), scan)
    if cut is not None:
        # Each channel is cut on its own and Sort-Object gives no stable order among equal times, so
        # a count of rows tied at the cut would not name the same rows next time. Leave the tied
        # rows to the next page instead (until_skip=0 reads them all), unless they are all there is.
        newer = [entry for entry in entries if entry.time > cut]
        if newer:
            entries = newer
            scan.continue_before(cut, 0)
        else:
            entries = [entry for entry in entries if entry.time == cut]  # older ones come on the next page
            scan.continue_before(cut, len(entries))
    return entries


MAX_IIS_CONFIG_BYTES = 4 * 1024 * 1024
MAX_IIS_BYTES = 16 * 1024 * 1024
MAX_IIS_FILES = 64
MAX_IIS_DIRECTORY_ENTRIES = 4096
MAX_IIS_ROWS = 20_000
# A clipped file is read from its tail, after its #Fields header. The header is taken from the
# file's head instead; IIS writes it again after a restart, so the last one in the head wins.
MAX_IIS_HEAD_BYTES = 64 * 1024
_W3C_REQUIRED = {b"date", b"time", b"cs-method", b"cs-uri-stem", b"sc-status"}


def _iis_config() -> Path:
    if sys.platform != "win32":
        raise Unavailable("IIS logging is available only on a classic Windows box")
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "inetsrv" / "config" / "applicationHost.config"


def _iis_folder(config: Path) -> tuple[Path, bool]:
    try:
        with config.open("rb") as stream:
            raw = stream.read(MAX_IIS_CONFIG_BYTES + 1)
    except FileNotFoundError:
        raise Unavailable("IIS applicationHost.config is absent") from None
    except OSError:
        raise Failed("log_io", "cannot read IIS applicationHost.config") from None
    if len(raw) > MAX_IIS_CONFIG_BYTES:
        raise Failed("limit", "IIS applicationHost.config exceeds 4 MiB")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise Failed("bad_reply", "invalid IIS applicationHost.config") from None
    host = root.find("system.applicationHost")
    sites = host.find("sites") if host is not None else None
    site = next((node for node in sites.findall("site") if node.get("name", "").casefold() == "aosservice"),
                None) if sites is not None else None
    if site is None:
        raise Unavailable("IIS site AOSService is absent")
    assert host is not None and sites is not None
    logging = host.find("log")
    if logging is not None and logging.get("centralLogFileMode", "Site").casefold() not in {"site", "0"}:
        raise Unavailable("AOSService does not use per-site IIS log files")
    values: dict[str, str] = {}
    for settings in (sites.find("siteDefaults/logFile"), site.find("logFile")):
        if settings is not None:
            values.update(settings.attrib)
    targets = {value.strip().casefold() for value in values.get("logTargetW3C", "File").split(",")}
    if (values.get("enabled", "true").casefold() in {"false", "0"}
            or values.get("logFormat", "W3C").casefold() not in {"w3c", "2"} or "file" not in targets):
        raise Unavailable("AOSService W3C file logging is disabled")
    site_id = site.get("id", "")
    directory = Path(os.path.expandvars(values.get("directory", r"%SystemDrive%\inetpub\logs\LogFiles")))
    if not re.fullmatch(r"[0-9]{1,10}", site_id) or not directory.is_absolute():
        raise Failed("bad_reply", "AOSService has an invalid IIS site id or log directory")
    local_rollover = values.get("localTimeRollover", "false").casefold() in {"true", "1"}
    return directory / f"W3SVC{site_id}", local_rollover


def _iis_file_start(name: str, local_rollover: bool) -> datetime | None:
    digits = name[4:-4].split("_", 1)[0]
    format_code = {4: "%y%m", 6: "%y%m%d", 8: "%y%m%d%H"}.get(len(digits))
    if format_code is None:
        return None
    try:
        start = datetime.strptime(digits, format_code).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # A local rollover name is only a coarse bound. W3C row timestamps are UTC.
    return start - timedelta(days=int(local_rollover))


def _iis_files(folder: Path, since: datetime, until: datetime, local_rollover: bool,
               deadline: float, scan: Scan) -> list[tuple[float, Path, int]]:
    # a continuation reads rows at exactly `until`, which can open a file of their own
    after_end = (lambda start: start <= until) if scan.inclusive else (lambda start: start < until)
    files = []
    present = False
    try:
        with os.scandir(folder) as paths:
            for index, item in enumerate(paths):
                if index == MAX_IIS_DIRECTORY_ENTRIES:
                    scan.notes.append("IIS directory scan reached its entry limit. Totals may be incomplete.")
                    break
                if time.monotonic() >= deadline:
                    raise Failed("timeout", "IIS log scan ran past its timeout")
                if not re.fullmatch(r"u_ex[0-9_]+\.log", item.name, re.IGNORECASE):
                    continue
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # a file rotated between enumeration and stat
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                present = True
                start = _iis_file_start(item.name, local_rollover)
                if info.st_mtime >= since.timestamp() and (start is None or after_end(start)):
                    files.append((info.st_mtime, Path(item.path), info.st_size))
    except FileNotFoundError:
        raise Unavailable("AOSService IIS log folder is absent") from None
    except OSError:
        raise Failed("log_io", "cannot list AOSService IIS log files") from None
    if not present and not scan.notes:
        raise Unavailable("AOSService has no W3C log files")
    if len(files) > MAX_IIS_FILES:
        scan.notes.append("IIS scan reached its file limit. Narrow since for complete totals.")
    return sorted(files, key=lambda item: (item[0], item[1].name), reverse=True)[:MAX_IIS_FILES]


_KEY_SEGMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\(|%28)")


def _normalise_key_segment(segment: str) -> str:
    match = _KEY_SEGMENT.match(segment)
    if match is None:
        return segment
    name = match[1]
    literal = unquote(segment[len(name):])
    depth = 0
    quoted = False
    for index, char in enumerate(literal):
        if char == "'":
            # Doubled apostrophes toggle twice and leave the string quoted.
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return name + "(…)" if index == len(literal) - 1 else segment
    return segment


def normalise_odata_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 3 or parts[0] or parts[1].casefold() != "data":
        return path
    return "/".join(parts[:2] + [_normalise_key_segment(part) for part in parts[2:]])


def _w3c_fields(line: bytes) -> list[bytes] | None:
    fields = line[8:].split()
    return fields if _W3C_REQUIRED.issubset(fields) and len(set(fields)) == len(fields) else None


def _head_fields(stream, tail_start: int) -> list[bytes] | None:
    """The last complete #Fields header before `tail_start`, within the file's first 64 KiB."""
    stream.seek(0)
    head = stream.read(min(tail_start, MAX_IIS_HEAD_BYTES))
    lines = head[:head.rfind(b"\n") + 1].removeprefix(b"\xef\xbb\xbf").splitlines()
    header = next((line for line in reversed(lines) if line.startswith(b"#Fields:")), None)
    return _w3c_fields(header) if header is not None else None


def _w3c_rows(data: bytes, since: datetime, until: datetime, deadline: float, group: bool, scan: Scan,
              fields: list[bytes] | None = None, skip: int = 0) -> tuple[list[Entry], bool, bool, int]:
    """Rows newest first (file order reversed), the newest MAX_IIS_ROWS after passing over the first
    `skip` rows stamped exactly `until`. Returns rows, invalid, row_cut and the rows skipped."""
    # room for the skipped rows: they are the newest lines at `until`, and must not push out older ones
    rows: deque[Entry] = deque(maxlen=MAX_IIS_ROWS + skip)
    fields = fields or []
    invalid = False
    row_cut = False
    # IIS may still be writing the final line. Only read newline-terminated records.
    for line in data[:data.rfind(b"\n") + 1].removeprefix(b"\xef\xbb\xbf").splitlines():
        if time.monotonic() >= deadline:
            raise Failed("timeout", "IIS log scan ran past its timeout")
        if line.startswith(b"#Fields:"):
            fields = _w3c_fields(line) or []
            invalid |= not fields
            continue
        if not line or line.startswith(b"#"):
            continue
        values = line.split()
        if not fields or len(values) != len(fields) or len(line) > 32_768:
            invalid = True
            continue
        row = dict(zip(fields, values))
        try:
            stamp = datetime.fromisoformat((row[b"date"] + b"T" + row[b"time"]).decode("ascii")).replace(tzinfo=timezone.utc)
            if not scan.in_window(stamp, since, until):
                continue
            method = row[b"cs-method"].decode("ascii")
            path = row[b"cs-uri-stem"].split(b"?", 1)[0].decode("utf-8", errors="replace")
            status = int(row[b"sc-status"])
            substatus = row.get(b"sc-substatus", b"-")
            elapsed = row.get(b"time-taken", b"-")
            elapsed_ms = None if elapsed == b"-" else int(elapsed)
            if not 0 <= status <= 999 or elapsed_ms is not None and not 0 <= elapsed_ms <= 0xffffffff:
                raise ValueError
            display_path = normalise_odata_path(path) if group else path
            context = {"method": method, "path": display_path, "status": status,
                       "substatus": None if substatus == b"-" else int(substatus), "time_taken": elapsed_ms}
            if group:
                context["example_path"] = path
        except (ValueError, OverflowError):
            invalid = True
            continue
        level = "error" if status >= 500 else "warning" if status >= 400 else "info"
        row_cut |= len(rows) == rows.maxlen
        rows.append(Entry(stamp, level, f"{method} {display_path} → HTTP {status}", context,
                          (method, display_path, status), elapsed_ms=elapsed_ms))
    newest = list(reversed(rows))
    kept = []
    for entry in newest:
        if skip and entry.time == until:
            skip -= 1
            continue
        kept.append(entry)
    skipped = len(newest) - len(kept)
    row_cut |= len(kept) > MAX_IIS_ROWS
    return kept[:MAX_IIS_ROWS], invalid, row_cut, skipped


def iis(since: datetime, until: datetime, deadline: float, scan: Scan, group: bool) -> list[Entry]:
    folder, local_rollover = _iis_folder(_iis_config())
    entries: list[Entry] = []
    left = MAX_IIS_BYTES
    invalid = False
    rotated = False
    skip = scan.until_skip or 0
    files = _iis_files(folder, since, until, local_rollover, deadline, scan)
    for index, (_, path, size) in enumerate(files):
        tail = size > left
        fields = None
        whole_first_line = not tail
        try:
            with path.open("rb") as stream:
                if tail:
                    fields = _head_fields(stream, size - left)
                    # a tail that starts right after a newline starts with a whole line
                    stream.seek(size - left - 1)
                    whole_first_line = stream.read(1) == b"\n"
                data = stream.read(min(size, left))
        except FileNotFoundError:
            rotated = True
            continue
        except OSError:
            raise Failed("log_io", "cannot read AOSService IIS log file") from None
        left -= len(data)
        if not whole_first_line:
            # Discard the partial line. Rows before a #Fields header in the tail use the head's.
            data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
        rows, bad_rows, row_cut, skipped = _w3c_rows(data, since, until, deadline, group, scan, fields, skip)
        skip -= skipped
        invalid |= bad_rows
        room = MAX_IIS_ROWS - len(entries)
        entries.extend(rows[:room])  # newest first: files newest first, lines from the end
        more_files = index + 1 < len(files)
        if row_cut or len(rows) > room or len(entries) == MAX_IIS_ROWS and more_files:
            scan.notes.append("IIS scan reached its row limit. Narrow since for complete totals.")
        if tail or left == 0 and more_files:
            scan.notes.append("IIS scan reached its byte limit. A clipped file uses the last #Fields header in its "
                              "first 64 KiB for rows before a header in the scanned tail.")
        if len(entries) == MAX_IIS_ROWS or tail or left == 0:
            break
    if scan.notes and entries:
        # the rows scanned so far are a newest-first prefix, ties included, so they count for until_skip
        oldest = min(entry.time for entry in entries)
        scan.continue_before(oldest, sum(entry.time == oldest for entry in entries))
    if invalid:
        scan.notes.append("Some IIS rows could not be read with the available W3C fields.")
    if rotated:
        scan.notes.append("An IIS log file rotated during the scan. Totals may be incomplete.")
    # stable: rows with equal times keep the newest-first file and line order that until_skip counts
    return sorted(entries, key=lambda entry: entry.time, reverse=True)
