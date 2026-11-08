"""Native file tools; registration owns presentation and roles, disk adapters own effects."""
from __future__ import annotations
from typing import Annotated, Any, Callable
from pydantic import Field
from boring_service_kit.tools import READ_ONLY, WRITES
from . import textfiles, binfiles, listing


def register_file_tools(tool: Callable[..., Any], call_budget: float | None = None) -> None:
    # Behind a Pond every call answers within its budget; leave room to answer after the walk.
    search_seconds = min(listing.SEARCH_SECONDS, 0.8 * call_budget) if call_budget else listing.SEARCH_SECONDS

    @tool(READ_ONLY, "reader")
    def file_read(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        start_line: Annotated[int, Field(
            description="First line, counted from 1.",
            json_schema_extra={"minimum": 1},
        )] = 1,
        line_count: Annotated[int, Field(
            description="Maximum lines to return.",
            json_schema_extra={"minimum": 1, "maximum": textfiles.READ_MAX_LINES},
        )] = textfiles.READ_DEFAULT_LINES,
    ) -> dict[str, Any]:
        ("Read text and its whole-file SHA-256. Call file_read with next_line to continue. Use "
         "file_read_bytes for binary.")
        return textfiles.read_text(path, start_line, line_count)

    @tool(WRITES, "writer")
    def file_write(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        content: str,
        expected_sha256: Annotated[str | None, Field(
            description="Whole-file SHA-256 from the read. Null creates a file.",
            json_schema_extra={"pattern": "^[0-9a-f]{64}$"},
        )],
        encoding: Annotated[str | None, Field(
            description="Omit to keep the encoding. New files default to UTF-8.",
            json_schema_extra={"enum": (*textfiles.ENCODINGS, None)},
        )] = None,
        bom: Annotated[bool | None, Field(description="Whether to write a byte order mark. UTF-16 requires it.")] = None,
        newline: Annotated[str | None, Field(
            description="Omit to retain line endings. Specify for mixed line endings.",
            json_schema_extra={"enum": (*textfiles.NEWLINES, None)},
        )] = None,
    ) -> dict[str, Any]:
        """Create or replace a text file. Read it first for the SHA-256 needed to replace it."""
        return textfiles.write_text(path, content, expected_sha256, encoding, bom, newline)

    @tool(WRITES, "writer")
    def file_patch(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        expected_sha256: Annotated[str, Field(
            description="Whole-file SHA-256 from file_read.",
            json_schema_extra={"pattern": "^[0-9a-f]{64}$"},
        )],
        edits: Annotated[list[dict[str, str]], Field(
            description=(
                "Objects with old_text and new_text, using the line endings shown by file_read. "
                "Empty new_text deletes the match."
            ),
        )],
        newline: Annotated[str | None, Field(
            description="Omit to retain line endings. Specify for mixed line endings.",
            json_schema_extra={"enum": (*textfiles.NEWLINES, None)},
        )] = None,
    ) -> dict[str, Any]:
        """Write exact replacements against a read file. Each old_text must match once and the matches cannot overlap."""
        return textfiles.patch_text(path, expected_sha256, edits, newline)

    @tool(READ_ONLY, "reader")
    def file_list(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        pattern: Annotated[str | None, Field(description="Glob on entry names.")] = None,
        limit: Annotated[int, Field(
            json_schema_extra={"minimum": 1, "maximum": listing.LIST_MAX},
        )] = listing.LIST_DEFAULT,
    ) -> dict[str, Any]:
        """Read one directory's entries with size and modification time."""
        return listing.list_dir(path, pattern, limit)

    @tool(READ_ONLY, "reader")
    def file_search(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        query: Annotated[str, Field(description="Nonempty literal on a single line.")],
        glob: Annotated[str | None, Field(description="Filename glob to restrict the search.")] = None,
        limit: Annotated[int, Field(
            json_schema_extra={"minimum": 1, "maximum": listing.SEARCH_MAX},
        )] = listing.SEARCH_DEFAULT,
        ignore_case: bool = False,
    ) -> dict[str, Any]:
        """Find literal text in a file or directory tree. Read matching lines with file_read."""
        return listing.search(path, query, glob, limit, ignore_case, search_seconds)

    @tool(READ_ONLY, "reader")
    def file_read_bytes(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        offset: Annotated[int, Field(
            description="Byte offset from the beginning.",
            json_schema_extra={"minimum": 0},
        )] = 0,
        length: Annotated[int, Field(
            description="Maximum bytes to return before base64 encoding.",
            json_schema_extra={"minimum": 1, "maximum": binfiles.CHUNK_MAX},
        )] = binfiles.CHUNK_MAX,
    ) -> dict[str, Any]:
        """Read a range as base64 and return the whole file's SHA-256. Continue at offset plus length."""
        return binfiles.read_bytes(path, offset, length)

    @tool(WRITES, "writer")
    def file_write_bytes(
        path: Annotated[str, Field(
            description="Path on the server machine. Relative paths use the service working directory.",
        )],
        data: Annotated[str, Field(description="Base64 payload, up to 1 MiB decoded.")],
        expected_sha256: Annotated[str | None, Field(
            description="Whole-file SHA-256 from the read. Null creates a file.",
            json_schema_extra={"pattern": "^[0-9a-f]{64}$"},
        )],
        offset: Annotated[int | None, Field(
            description="Byte offset. Omit to replace the whole file.",
            json_schema_extra={"minimum": 0},
        )] = None,
    ) -> dict[str, Any]:
        ("Create or write raw bytes, optionally at an offset. Read the file first to get its SHA-256 before "
         "changing it.")
        return binfiles.write_bytes(path, data, expected_sha256, offset)
