"""Probe: are the real index's label references complete and exact? (run with the release's Python)

One naive pass over the source tree, independent of the index's reference
extraction, compared with the index:

- every label reference candidate (`@Id`, `@File:Id`) in non-label files whose
  label exists -- total against the index's `label_refs`;
- per reference, for fixed short IDs (`@SYS1`, `@SYS10`, `@SYS100`, `@SYS7149`)
  and a random sample of 25 references from the index;
- the forms references take: inside `literalStr(...)`, as an XML property
  (by tag name), elsewhere in code;
- candidates whose label does not exist (typos, other environments, or forms
  we do not resolve);
- labels whose text has placeholders (`%1`).

Known label IDs come from the en-US label files, read directly; references to a
label without an en-US text are counted separately.

Writes numbers only -- no file contents, element or customer names.
Sample references are reported by count and agreement, not by ID.

    python probe_labels.py <result.json> [<db> <PackagesLocalDirectory>]
"""
import collections
import json
import random
import re
import sys
import time
from pathlib import Path

from boring_sourcecache_d365 import sourcecache

DB = sys.argv[2] if len(sys.argv) > 2 else r"J:\pondnt\d365-devbox.db"
SOURCE = Path(sys.argv[3] if len(sys.argv) > 3 else r"J:\AOSService\PackagesLocalDirectory")
FIXED = ["@SYS1", "@SYS10", "@SYS100", "@SYS7149"]
SAMPLE = 25

# Independent of sourcecache.LABEL_REF on purpose: written from the label syntax, not copied.
CANDIDATE = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?(?![A-Za-z0-9_])")
XML_PROPERTY = re.compile(r"<([A-Za-z]+)>\s*$")
started = time.perf_counter()
result = {"db": DB}
index = sourcecache.Index(DB, stall_seconds=0)
db = index.db

# Known label IDs straight from the en-US label files (naive, sequential): scanning the index's labels
# table reads pages scattered over the whole file -- ~1 MB/s on a dev box's disk.
t0 = time.perf_counter()
known = set()
placeholders = label_files = 0
for path in sourcecache._walk(SOURCE):
    parts = path.relative_to(SOURCE).parts
    keys = sourcecache.classify(parts)
    if keys["kind"] != "label" or (keys["locale"] or "").lower() != "en-us":
        continue
    try:
        text = sourcecache._text(path.read_bytes())
    except OSError:
        continue
    if text is None:
        continue
    label_files += 1
    for line in text.split("\n"):
        if "=" not in line or line.lstrip().startswith(";"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        known.add((key if key.startswith("@") else f"@{keys['label_file']}:{key}").lower())
        if "%1" in value:
            placeholders += 1
result["en_us_label_files"] = label_files
result["distinct_label_ids_en_us"] = len(known)
result["placeholder_labels_en_us"] = placeholders
result["load_label_ids_s"] = round(time.perf_counter() - t0, 1)
result["index_label_refs"] = db.execute("SELECT COUNT(*) FROM label_refs").fetchone()[0]

random.seed(20261005)
referenced = sorted(r[0] for r in db.execute("SELECT DISTINCT lower(ref) FROM label_refs"))
sample = set(random.sample(referenced, min(SAMPLE, len(referenced))))
result["distinct_referenced_ids"] = len(referenced)
result["referenced_without_en_us_label"] = sum(r not in known for r in referenced)
watched = {r.lower() for r in FIXED} | sample
index_counts = {ref: db.execute("SELECT COUNT(*) FROM label_refs WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()[0]
                for ref in watched}

naive_total = naive_unknown = files = 0
naive_counts = collections.Counter()
forms = collections.Counter()
xml_tags = collections.Counter()
t0 = time.perf_counter()
for path in sourcecache._walk(SOURCE):
    parts = path.relative_to(SOURCE).parts
    if sourcecache.classify(parts)["kind"] == "label":
        continue
    try:
        if path.stat().st_size > sourcecache.MAX_FILE_BYTES:
            continue
        text = sourcecache._text(path.read_bytes())
    except OSError:
        continue
    if text is None or "@" not in text:
        continue
    files += 1
    for m in CANDIDATE.finditer(text):
        ref = ("@" + m.group(1) + (":" + m.group(2) if m.group(2) else "")).lower()
        if ref not in known:
            naive_unknown += 1
            continue
        naive_total += 1
        if ref in watched:
            naive_counts[ref] += 1
        before = text[max(0, m.start() - 40):m.start()]
        if "literalstr(" in before.lower().replace(" ", ""):
            forms["literalStr"] += 1
        else:
            tag = XML_PROPERTY.search(before)
            if tag:
                forms["xml_property"] += 1
                xml_tags[tag.group(1)] += 1
            else:
                forms["other_in_code_or_xml_text"] += 1

result["naive"] = {"files_with_at": files, "refs_to_existing_labels": naive_total,
                   "candidates_without_label": naive_unknown, "seconds": round(time.perf_counter() - t0)}
result["agree_total"] = naive_total == result["index_label_refs"]
result["fixed"] = {ref: {"index": index_counts[ref.lower()], "naive": naive_counts[ref.lower()],
                         "agree": index_counts[ref.lower()] == naive_counts[ref.lower()]} for ref in FIXED}
agreeing = sum(index_counts[r] == naive_counts[r] for r in sample)
result["sample"] = {"refs": len(sample), "agree": agreeing,
                    "index_total": sum(index_counts[r] for r in sample),
                    "naive_total": sum(naive_counts[r] for r in sample)}
result["forms"] = dict(forms)
result["xml_tags_top"] = xml_tags.most_common(15)
result["seconds"] = round(time.perf_counter() - started)

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1)
print("probe written")
