"""Text files that keep their encoding, BOM and line endings.

Copilot sees text with `\n` line endings. On disk, a file keeps what it had:
UTF-8 with or without BOM, UTF-16 LE or BE (with BOM), CRLF or LF. Every read
returns the sha256 of the file's bytes; every write and patch names the hash
it expects, so an edit never overwrites a change it has not seen: the version
checked against that hash travels to publication and is checked again on the
handle written through (disk.write_contents).

No guessing of legacy code pages: a file that is not one of these encodings is
refused; the byte tools handle it.

Format policy is shared through boring-file-core.textformat. This adapter
keeps pondNT disk access, limits and outcomes. Edit semantics follow the Pond: `invalid_request`, `forbidden`, `not_found`, `stale` (with
`current_sha256`), `limit`. A null expected hash means "I believe the file is
absent"; anything else is stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import disk
from .disk import sha256_bytes
from boring_service_kit.outcomes import Refused

from boring_file_core import textformat
from boring_file_core.textpatch import PatchError, apply_edits, checked_edits

UTF8_BOM = textformat.UTF8_BOM
UTF16_LE_BOM = textformat.UTF16_LE_BOM
UTF16_BE_BOM = textformat.UTF16_BE_BOM
UTF32_LE_BOM = textformat.UTF32_LE_BOM
TextFormat = textformat.TextFormat
ENCODINGS = textformat.ENCODINGS
NEWLINES = textformat.NEWLINES
normalize_newlines = textformat.normalize_newlines
detect_newline = textformat.detect_newline
NEW_FILE_ENCODING = "utf-8"
NEW_FILE_BOM = False
NEW_FILE_NEWLINE = "crlf"
READ_DEFAULT_LINES = 400
READ_MAX_LINES = 2000
READ_MAX_CHARS = 64 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Decoded:
    text: str
    format: TextFormat
    sha256: str
    size: int


def decode(data: bytes) -> Decoded:
    try:
        value = textformat.decode(data)
    except textformat.TextFormatError as error:
        raise Refused("invalid_request", str(error), size=len(data)) from None
    return Decoded(value.text, value.format, sha256_bytes(data), len(data))


def encode(text: str, fmt: TextFormat) -> bytes:
    try:
        return textformat.encode(text, fmt)
    except (textformat.TextFormatError, UnicodeEncodeError) as error:
        raise Refused("invalid_request", str(error)) from None


def _read_text_bytes(p) -> bytes:
    return _read_text_version(p)[0]


def _read_text_version(p) -> tuple[bytes, disk.Version]:
    return disk.read_version(p, MAX_FILE_BYTES, "; use the byte tools")


def _check_size(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise Refused("limit", f"result exceeds {MAX_FILE_BYTES} bytes")


# --- tools -------------------------------------------------------------------


def check_read(start_line: int, line_count: int) -> None:
    if start_line < 1:
        raise Refused("invalid_request", "start_line starts at 1")
    if not 1 <= line_count <= READ_MAX_LINES:
        raise Refused("invalid_request", f"line_count must be 1-{READ_MAX_LINES}")


def read_text(path: str, start_line: int = 1, line_count: int = READ_DEFAULT_LINES) -> dict[str, Any]:
    check_read(start_line, line_count)
    p = disk.absolute(path)
    decoded = decode(_read_text_bytes(p))
    lines = decoded.text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a final line break does not start another line
    total = len(lines)
    selected: list[str] = []
    chars = 0
    line_cut = False
    index = start_line - 1
    while index < total and len(selected) < line_count:
        line = lines[index]
        if chars + len(line) + 1 > READ_MAX_CHARS:
            if not selected:  # one line longer than the limit: return its start
                selected.append(line[:READ_MAX_CHARS])
                line_cut = True
                index += 1
            break
        selected.append(line)
        chars += len(line) + 1
        index += 1
    next_line = index + 1 if index < total else None
    result: dict[str, Any] = {
        "path": str(p),
        "content": "".join(line + "\n" for line in selected),
        "start_line": start_line,
        "returned_lines": len(selected),
        "total_lines": total,
        "next_line": next_line,
        "truncated": next_line is not None or line_cut,
        "sha256": decoded.sha256,
        "size": decoded.size,
        **decoded.format.as_dict(),
    }
    if line_cut:
        result["line_cut"] = True
    return result


def target_format(
    existing: TextFormat | None, encoding: str | None, bom: bool | None, newline: str | None
) -> TextFormat:
    try:
        return textformat.target_format(existing, encoding, bom, newline, default_newline=NEW_FILE_NEWLINE)
    except textformat.TextFormatError as error:
        raise Refused("invalid_request", str(error)) from None


@disk.locked_mutation
def _write_text(
    path: str,
    content: str,
    expected_sha256: str | None,
    encoding: str | None = None,
    bom: bool | None = None,
    newline: str | None = None,
    *, publisher,
) -> dict[str, Any]:
    p = disk.absolute(path)
    existing: TextFormat | None = None
    version: disk.Version | None = None
    if expected_sha256 is None:
        if p.exists():
            current = sha256_bytes(_read_text_bytes(p)) if p.is_file() else None
            raise Refused("stale", "file exists; read it and pass its sha256 to replace it", path=str(p),
                          current_sha256=current)
        disk.require_parent(p)
    else:
        data, version = _read_text_version(p)
        current = version.sha256
        if current != expected_sha256:
            raise Refused("stale", "file changed since it was read", path=str(p), current_sha256=current)
        try:
            existing = decode(data).format
        except Refused:
            existing = None  # replacing a non-text file: new-file defaults apply
    fmt = target_format(existing, encoding, bom, newline)
    new_data = encode(content, fmt)
    _check_size(new_data)
    disk.require_writable(p)
    publisher(p, new_data, create=expected_sha256 is None, expected=version)
    return {"path": str(p), "sha256": sha256_bytes(new_data), "size": len(new_data), **fmt.as_dict()}


def write_text(
    path: str,
    content: str,
    expected_sha256: str | None,
    encoding: str | None = None,
    bom: bool | None = None,
    newline: str | None = None,
) -> dict[str, Any]:
    return _write_text(path, content, expected_sha256, encoding, bom, newline,
                       publisher=disk.write_contents)


def check_patch(edits: list[dict[str, str]]) -> list[tuple[str, str]]:
    try:
        return checked_edits([(e.get("old_text", ""), e.get("new_text", "")) for e in edits])
    except PatchError as error:
        raise Refused("invalid_request", str(error), **error.details) from None


@disk.locked_mutation
def patch_text(
    path: str,
    expected_sha256: str,
    edits: list[dict[str, str]],
    newline: str | None = None,
) -> dict[str, Any]:
    """Apply exact-text edits, all or nothing.

    Like the Pond's file_patch: every `old_text` must occur exactly once in the
    file as read, and the matches must not overlap, so the order of edits does
    not matter. Matching uses `\n` line endings, as `read_text` shows them.
    """
    prepared = check_patch(edits)
    p = disk.absolute(path)
    data, version = _read_text_version(p)
    current = version.sha256
    if current != expected_sha256:
        raise Refused("stale", "file changed since it was read", path=str(p), current_sha256=current)
    decoded = decode(data)
    fmt = target_format(decoded.format, None, None, newline)
    try:
        text = apply_edits(decoded.text, prepared)
    except PatchError as error:
        raise Refused("invalid_request", str(error), **error.details) from None
    new_data = encode(text, fmt)
    _check_size(new_data)
    disk.require_writable(p)
    disk.write_contents(p, new_data, expected=version)
    return {
        "path": str(p),
        "sha256": sha256_bytes(new_data),
        "size": len(new_data),
        "applied_edits": len(edits),
        **fmt.as_dict(),
    }
