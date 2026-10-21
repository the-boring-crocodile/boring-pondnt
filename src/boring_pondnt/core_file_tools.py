"""Native file tools; registration owns presentation and roles, disk adapters own effects."""
from __future__ import annotations
from typing import Any, Callable
from boring_service_kit.tools import READ_ONLY, WRITES
from . import textfiles, binfiles, listing


def register_file_tools(tool: Callable[..., Any]) -> None:
    @tool(READ_ONLY, "reader")
    def file_read(path: str, start_line: int = 1,
                  line_count: int = textfiles.READ_DEFAULT_LINES) -> dict[str, Any]:
        """Read a text file (UTF-8/UTF-16, any BOM, CRLF/LF) as `\\n` text, by lines, with its sha256."""
        return textfiles.read_text(path, start_line, line_count)

    @tool(WRITES, "writer")
    def file_write(path: str, content: str, expected_sha256: str | None, encoding: str | None = None,
                   bom: bool | None = None, newline: str | None = None) -> dict[str, Any]:
        """Create (expected_sha256 null) or replace text; format kept. newline crlf|lf, encoding utf-8|utf-16-le|utf-16-be."""
        return textfiles.write_text(path, content, expected_sha256, encoding, bom, newline)

    @tool(WRITES, "writer")
    def file_patch(path: str, expected_sha256: str, edits: list[dict[str, str]],
                   newline: str | None = None) -> dict[str, Any]:
        """Replace exact texts: each old_text once in the file as read, no overlaps. Format kept. newline: crlf|lf."""
        return textfiles.patch_text(path, expected_sha256, edits, newline)

    @tool(READ_ONLY, "reader")
    def file_list(path: str, pattern: str | None = None, limit: int = listing.LIST_DEFAULT) -> dict[str, Any]:
        """List one directory: name, type, size, modified. Optional glob pattern."""
        return listing.list_dir(path, pattern, limit)

    @tool(READ_ONLY, "reader")
    def file_search(path: str, query: str, glob: str | None = None, limit: int = listing.SEARCH_DEFAULT,
                    ignore_case: bool = False) -> dict[str, Any]:
        """Literal search in a file or small tree. For the D365 source, use Sourcecache."""
        return listing.search(path, query, glob, limit, ignore_case)

    @tool(READ_ONLY, "reader")
    def file_read_bytes(path: str, offset: int = 0, length: int = binfiles.CHUNK_MAX) -> dict[str, Any]:
        """Read raw bytes as base64, by range, with the whole file's sha256."""
        return binfiles.read_bytes(path, offset, length)

    @tool(WRITES, "writer")
    def file_write_bytes(path: str, data: str, expected_sha256: str | None,
                         offset: int | None = None) -> dict[str, Any]:
        """Write base64 bytes: whole file (offset null) or over a range. expected_sha256 null creates."""
        return binfiles.write_bytes(path, data, expected_sha256, offset)
