"""Probe: does the real index agree with the real files? (run with the release's Python)

Opens the index read-only and compares its answers with a naive scan of the
source tree -- an earlier prototype's cross-check (AllowLocOverridePut) and the
label case that broke it (references to @SYS1). Writes numbers and timings only, no
file contents or element names.

    python probe_index.py <result.json> [<db> <PackagesLocalDirectory>]
"""
import json
import re
import sys
import time
from pathlib import Path

from boring_sourcecache_d365 import sourcecache

DB = sys.argv[2] if len(sys.argv) > 2 else r"J:\pondnt\d365-devbox.db"
SOURCE = Path(sys.argv[3] if len(sys.argv) > 3 else r"J:\AOSService\PackagesLocalDirectory")
NEEDLE = "AllowLocOverridePut"
REF = "@SYS1"


def timed(fn):
    """(value, milliseconds); a refusal or error becomes the value -- a probe reports, it does not stop."""
    start = time.perf_counter()
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        value = {"error": f"{type(exc).__name__}: {exc}"}
    return value, round((time.perf_counter() - start) * 1000, 1)


result = {"db": DB, "exists": Path(DB).is_file()}
if result["exists"]:
    index = sourcecache.Index(DB)
    meta = index.meta
    result["meta"] = {k: meta.get(k) for k in ("name", "application_version", "platform_version", "built",
                                                "files", "labels", "label_refs", "skipped", "models")}
    result["db_mb"] = round(Path(DB).stat().st_size / 1e6)

    search, ms = timed(lambda: index.search(NEEDLE, limit=100))
    result["index_search_files"] = search.get("total_files", search)
    result["index_search_ms"] = ms
    refs, ms = timed(lambda: index.labels(label_id=REF, references=True, limit=1))
    result["index_ref_total"] = refs.get("references_total", refs)
    result["index_ref_ms"] = ms
    for label, fn in [("common word 'SalesTable'", lambda: index.search("SalesTable", limit=20)),
                      ("label text 'Customer account'", lambda: index.labels(text="Customer account", limit=20)),
                      ("element AxTable SalesTable", lambda: index.element("AxTable", "SalesTable"))]:
        _, ms = timed(fn)
        result.setdefault("query_ms", {})[label] = ms

    # The naive scan: the same files the builder takes, read as text, no index.
    pattern = re.compile(r"(?<![A-Za-z0-9_@])" + re.escape(REF) + r"(?![A-Za-z0-9_]|:[A-Za-z_])", re.IGNORECASE)
    needle = NEEDLE.lower()
    files = matches = refs_found = 0
    start = time.perf_counter()
    for path in sourcecache._walk(SOURCE):
        parts = path.relative_to(SOURCE).parts
        try:
            if path.stat().st_size > sourcecache.MAX_FILE_BYTES:
                continue
            text = sourcecache._text(path.read_bytes())
        except OSError:
            continue
        if text is None:
            continue
        files += 1
        if needle in text.lower():
            matches += 1
        if sourcecache.classify(parts)["kind"] != "label" and "@" in text:
            refs_found += len(pattern.findall(text))  # every occurrence, as the index records them
    result["naive"] = {"files": files, "search_files": matches, "ref_total": refs_found,
                       "seconds": round(time.perf_counter() - start)}
    result["agree"] = {"files": files == int(meta.get("files", -1)),
                       "search": matches == result["index_search_files"],
                       "refs": refs_found == result["index_ref_total"]}

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1)
print("probe written")
