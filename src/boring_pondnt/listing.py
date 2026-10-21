"""Directory listing and literal search, bounded.

For small trees. The D365 source has Sourcecache (boring-sourcecache-d365); this walks the
disk and stops at its limits rather than pretending to be an index.
"""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import disk
from boring_service_kit.outcomes import Refused
from .textfiles import MAX_FILE_BYTES, decode

LIST_DEFAULT = 200
LIST_MAX = 1000
SEARCH_DEFAULT = 100
SEARCH_MAX = 500
SEARCH_MAX_FILES = 20_000  # files examined per call; the walk stops beyond it
SNIPPET_CHARS = 200
SKIP_DIRS = {".git", ".vs", "node_modules", "__pycache__"}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _directory(path: str) -> Path:
    p = disk.absolute(path)
    if not p.exists():
        raise Refused("not_found", "directory does not exist", path=str(p))
    if not p.is_dir():
        raise Refused("invalid_request", "path is not a directory", path=str(p))
    return p


def check_list(limit: int) -> None:
    if not 1 <= limit <= LIST_MAX:
        raise Refused("invalid_request", f"limit must be 1-{LIST_MAX}")


def check_search(query: str, limit: int) -> None:
    if not query:
        raise Refused("invalid_request", "query must be nonempty")
    if "\n" in query or "\r" in query:
        raise Refused("invalid_request", "query is matched within one line")
    if not 1 <= limit <= SEARCH_MAX:
        raise Refused("invalid_request", f"limit must be 1-{SEARCH_MAX}")


def list_dir(path: str, pattern: str | None = None, limit: int = LIST_DEFAULT) -> dict[str, Any]:
    """One directory level, sorted by name (case-insensitive), dirs and files alike."""
    check_list(limit)
    p = _directory(path)
    try:
        names = os.listdir(p)
    except PermissionError:
        raise Refused("forbidden", "directory is not accessible", path=str(p)) from None
    if pattern:
        names = [n for n in names if fnmatch.fnmatch(n.lower(), pattern.lower())]
    names.sort(key=lambda n: (n.lower(), n))
    entries = []
    for name in names[:limit]:
        entry: dict[str, Any] = {"name": name}
        try:
            st = (p / name).lstat()
        except OSError:  # removed while listing, or unreadable
            entry["type"] = "unknown"
            entries.append(entry)
            continue
        if (p / name).is_symlink():
            entry["type"] = "link"
        elif (p / name).is_dir():
            entry["type"] = "dir"
        else:
            entry["type"] = "file"
            entry["size"] = st.st_size
        entry["modified"] = _iso(st.st_mtime)
        entries.append(entry)
    return {"path": str(p), "entries": entries, "total": len(names), "truncated": len(names) > limit}


def search(
    path: str,
    query: str,
    glob: str | None = None,
    limit: int = SEARCH_DEFAULT,
    ignore_case: bool = False,
) -> dict[str, Any]:
    """Literal text search in one file or a tree. Lines are shown as `read_text` shows them."""
    check_search(query, limit)
    root = disk.absolute(path)
    if not root.exists():
        raise Refused("not_found", "path does not exist", path=str(root))
    needle = query.lower() if ignore_case else query
    matches: list[dict[str, Any]] = []
    examined = skipped = 0
    stopped = None  # why the walk ended early, if it did

    def files():
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if glob is None or fnmatch.fnmatch(name.lower(), glob.lower()):
                    yield Path(dirpath) / name

    for file in files():
        if examined >= SEARCH_MAX_FILES:
            stopped = "max_files"
            break
        examined += 1
        try:
            text = decode(disk.read_bytes(file, MAX_FILE_BYTES)).text
        except (OSError, Refused):  # unreadable, binary, or gone
            skipped += 1
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            haystack = line.lower() if ignore_case else line
            if needle in haystack:
                if len(matches) == limit:
                    stopped = "limit"
                    break
                matches.append({"path": str(file), "line": number, "text": line.strip()[:SNIPPET_CHARS]})
        if stopped == "limit":
            break
    return {
        "path": str(root),
        "matches": matches,
        "files_examined": examined,
        "files_skipped": skipped,
        "truncated": stopped is not None,
        "stopped": stopped,
    }
