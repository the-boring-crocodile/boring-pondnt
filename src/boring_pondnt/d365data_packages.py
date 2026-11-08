"""Confined, bounded DMF package files; no ZIP extraction."""
from __future__ import annotations

import io
import os
import stat
import uuid
import zipfile
from pathlib import Path

from boring_service_kit.outcomes import Failed, Refused

MAX_PACKAGE_BYTES = 64 * 1024 * 1024


def package_path(folder: str, name: str | None = None) -> Path:
    """Reject traversal and links (including Windows junctions) before opening."""
    if not folder:
        raise Refused("invalid_request", "package_folder must be configured")
    root = Path(folder)
    if not root.is_absolute() or ".." in root.parts:
        raise Refused("invalid_request", "package_folder must be an absolute path without ..")
    target = root
    if name is not None:
        if not isinstance(name, str) or not name or len(name) > 4000:
            raise Refused("invalid_request", "package_path must be a nonempty path")
        candidate = Path(name)
        if ".." in candidate.parts:
            raise Refused("forbidden", "package traversal is not allowed")
        target = candidate if candidate.is_absolute() else root / candidate
        if not target.is_relative_to(root) or target == root:
            raise Refused("forbidden", "package_path must be inside package_folder")
        if target.suffix.lower() != ".zip":
            raise Refused("invalid_request", "package_path must name a ZIP file")
        # Also reject alternate streams and Windows device paths on portable tests.
        relative = target.relative_to(root)
        if any(":" in part or "\\" in part or part.endswith((" ", ".")) for part in relative.parts):
            raise Refused("forbidden", "unsupported package path")
    try:
        for path in reversed([target, *target.parents]):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Refused("forbidden", "package paths must not contain links or reparse points")
        if not root.is_dir():
            raise Refused("invalid_request", "package_folder must be an existing directory")
    except OSError:
        raise Refused("not_found", "package folder or file is unavailable") from None
    return target


def _check_opened(folder: str, path: Path, info: os.stat_result) -> None:
    # The lstat walk and the open are separate steps: an ancestor swapped for a
    # junction in between (Windows has no O_NOFOLLOW for directories) would go
    # unnoticed. Resolve the path now and require it to be the file we hold.
    root = Path(os.path.realpath(folder))
    real = Path(os.path.realpath(path))
    if not real.is_relative_to(root) or real == root:
        raise Refused("forbidden", "package path resolved outside package_folder while it was opened")
    resolved = os.stat(real)
    if (resolved.st_dev, resolved.st_ino) != (info.st_dev, info.st_ino):
        raise Refused("forbidden", "package path changed while it was opened")


def validate_zip(data: bytes) -> None:
    if not data or len(data) > MAX_PACKAGE_BYTES:
        raise Refused("limit", "package must be nonempty and at most 64 MiB")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise Refused("invalid_request", "package is not a ZIP file")


def read_package(folder: str, name: str) -> bytes:
    path = package_path(folder, name)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused("forbidden", "package must be a regular file with one link")
            if info.st_size > MAX_PACKAGE_BYTES:
                raise Refused("limit", "package exceeds 64 MiB")
            _check_opened(folder, path, info)
            data = stream.read(MAX_PACKAGE_BYTES + 1)
    except OSError:
        raise Failed("package_io", "cannot read package file") from None
    validate_zip(data)
    return data


def save_package(folder: str, data: bytes) -> dict[str, object]:
    try:
        validate_zip(data)
    except Refused:
        raise Failed("bad_reply", "download is not a valid bounded ZIP package") from None
    root = package_path(folder)
    path = root / (uuid.uuid4().hex + ".zip")
    created = False
    try:
        # An execution id is never a filename; exclusive creation never replaces a file.
        with path.open("xb") as stream:
            created = True
            stream.write(data)
        return {"package_path": str(path), "package_bytes": len(data)}
    except OSError:
        if created:
            try:
                path.unlink()
            except OSError:
                raise Failed("package_io", "package write failed; partial file cleanup failed",
                             package_path=str(path)) from None
        raise Failed("package_io", "cannot save downloaded package") from None
