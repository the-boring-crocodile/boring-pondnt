"""Raw bytes, ranged, as base64.

For what the text tools refuse: images, compiled resources, other encodings.
Every answer carries the whole file's sha256, so the hash rule is the same
as for text: a write names the hash it expects, null to create.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from . import disk
from .disk import sha256_bytes
from boring_service_kit.outcomes import Refused

CHUNK_MAX = 1024 * 1024  # bytes per read or write call
FILE_MAX = 64 * 1024 * 1024  # files these tools will read whole or splice


def check_read(offset: int, length: int) -> None:
    if offset < 0:
        raise Refused("invalid_request", "offset must be 0 or more")
    if not 1 <= length <= CHUNK_MAX:
        raise Refused("invalid_request", f"length must be 1-{CHUNK_MAX}")


def decode_write(data: str, expected_sha256: str | None, offset: int | None) -> bytes:
    if offset is not None and offset < 0:
        raise Refused("invalid_request", "offset must be 0 or more")
    if expected_sha256 is None and offset not in (None, 0):
        raise Refused("invalid_request", "a new file starts at offset 0")
    try:
        chunk = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refused("invalid_request", "data is not valid base64") from None
    if len(chunk) > CHUNK_MAX:
        raise Refused("limit", f"data exceeds {CHUNK_MAX} bytes per call")
    return chunk


def read_bytes(path: str, offset: int = 0, length: int = CHUNK_MAX) -> dict[str, Any]:
    check_read(offset, length)
    p = disk.absolute(path)
    data = disk.read_bytes(p, FILE_MAX)
    chunk = data[offset:offset + length]
    end = offset + len(chunk)
    return {
        "path": str(p),
        "data": base64.b64encode(chunk).decode("ascii"),
        "offset": offset,
        "length": len(chunk),
        "size": len(data),
        "eof": end >= len(data),
        "sha256": sha256_bytes(data),
    }


@disk.locked_mutation
def write_bytes(
    path: str, data: str, expected_sha256: str | None, offset: int | None = None
) -> dict[str, Any]:
    """Write base64 `data`: the whole file (offset null), or over a range from `offset`.

    A range may extend the file; writing past its end is refused, so no gap
    is ever filled with zeros silently.
    """
    chunk = decode_write(data, expected_sha256, offset)
    p = disk.absolute(path)
    version: disk.Version | None = None
    if expected_sha256 is None:
        if p.exists():
            current = sha256_bytes(disk.read_bytes(p, FILE_MAX)) if p.is_file() else None
            raise Refused("stale", "file exists; read it and pass its sha256", path=str(p),
                          current_sha256=current)
        disk.require_parent(p)
        new = chunk
    else:
        old, version = disk.read_version(p, FILE_MAX)
        current = version.sha256
        if current != expected_sha256:
            raise Refused("stale", "file changed since it was read", path=str(p), current_sha256=current)
        if offset is None:
            new = chunk
        elif offset > len(old):
            raise Refused("invalid_request", "offset is past the end of the file", size=len(old))
        else:
            new = old[:offset] + chunk + old[offset + len(chunk):]
    if len(new) > FILE_MAX:
        raise Refused("limit", f"result exceeds {FILE_MAX} bytes")
    disk.require_writable(p)
    disk.write_contents(p, new, create=expected_sha256 is None, expected=version)
    return {"path": str(p), "size": len(new), "sha256": sha256_bytes(new)}
