"""The disk layer: absolute paths, bounded reads, in-place edits and exclusive creation.

Deliberately plain: no roots, no sandbox. This layer does not
travel to the Pond; the Pond's own disk layer is stricter for good
reasons. Format logic lives in `textfiles`; Windows specifics stay here,
recognised by their error codes, so the module also runs on Linux.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import shutil
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from boring_service_kit.filelock import file_lock

from boring_service_kit.outcomes import Failed, Refused
from .windows_files import SecurityFinalizationError

# Windows sharing and lock violations (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION).
WINDOWS_LOCK_ERRORS = {32, 33}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_lock_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in WINDOWS_LOCK_ERRORS or exc.errno == errno.EBUSY


def absolute(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise Refused("invalid_request", "path must be absolute", path=path)
    return p


@dataclass(frozen=True)
class Version:
    """The file an edit was checked against: its identity and its content.

    Carried from the hash check to publication, where `write_contents` checks
    it again on the handle it writes through.
    """
    dev: int
    ino: int
    size: int
    sha256: str


def read_bytes(p: Path, limit: int, hint: str = "") -> bytes:
    """Read a whole file of at most `limit` bytes, or refuse."""
    return _read(p, limit, hint, identify=False)[0]


def read_version(p: Path, limit: int, hint: str = "") -> tuple[bytes, Version]:
    """Read a whole file of at most `limit` bytes with the identity of the handle read, or refuse."""
    data, info = _read(p, limit, hint, identify=True)
    return data, Version(info.st_dev, info.st_ino, len(data), sha256_bytes(data))


def _read(p: Path, limit: int, hint: str, identify: bool):
    try:
        size = p.stat().st_size
        if p.is_dir():
            raise Refused("invalid_request", "path is a directory", path=str(p))
        if size > limit:
            raise Refused("limit", f"file exceeds {limit} bytes{hint}", path=str(p), size=size)
        with p.open("rb") as handle:
            info = os.fstat(handle.fileno()) if identify else None
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise Refused("limit", f"file exceeds {limit} bytes{hint}", path=str(p))
        return data, info
    except FileNotFoundError:
        raise Refused("not_found", "file does not exist", path=str(p)) from None
    except PermissionError as exc:
        if is_lock_error(exc):
            raise Refused("locked", "file is locked by another process", path=str(p)) from None
        raise Refused("forbidden", "file is not accessible", path=str(p)) from None


def require_parent(p: Path) -> None:
    if not p.parent.is_dir():
        raise Refused("not_found", "directory does not exist", path=str(p.parent))


def require_writable(p: Path) -> None:
    """Refuse a read-only target before any effect.

    TFVC keeps files read-only until checked out. Windows then refuses the
    replace, while POSIX would allow it (replacing depends on the folder), so
    the same edit would differ per platform; checking the file's own write
    bit makes both refuse, with a message that says what to do.
    """
    try:
        mode = p.stat().st_mode
    except FileNotFoundError:
        return
    if not mode & stat.S_IWUSR:
        raise Refused("forbidden", "file is read-only; if it is under TFVC, check it out first", path=str(p))


def locked_mutation(function):
    """Serialize cooperating processes around hash checks and publication."""
    @wraps(function)
    def call(path, *args, **kwargs):
        target = absolute(path)
        identity = os.path.normcase(str(target.resolve()))
        suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
        folder = Path(tempfile.gettempdir()) / ("pondnt-edit-locks-" + suffix)
        folder.mkdir(mode=0o700, exist_ok=True)
        info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or folder.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
            raise Refused("forbidden", "invalid edit lock directory")
        with file_lock(folder / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")):
            mutation_metadata(target)
            return function(path, *args, **kwargs)
    return call


def mutation_metadata(p: Path):
    """Refuse links/streams before effects; ordinary files retain metadata."""
    if os.name == "nt" and ":" in os.path.splitdrive(str(p))[1]:
        raise Refused("forbidden", "writing a named stream directly is unsupported", path=str(p))
    try:
        info = p.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise Refused("forbidden", "mutation requires a regular single-link non-reparse file", path=str(p))
    if os.name != "nt" and hasattr(os, "listxattr") and os.listxattr(p, follow_symlinks=False):
        raise Refused("forbidden", "file has unsupported extended metadata", path=str(p))
    return info


def _overwrite(handle, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        count = handle.write(view[offset:])
        if not count:
            raise OSError(errno.EIO, "write made no progress")
        offset += count
    handle.truncate(len(data))


def _read_handle(handle, size: int) -> bytes:
    """Up to `size + 1` bytes from the start: enough to tell a different length."""
    chunks, remaining = [], size + 1
    while remaining > 0:
        chunk = handle.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_version(handle, p: Path, expected: Version) -> None:
    """Refuse unless the opened file is the one read and still holds what was read."""
    current = os.fstat(handle.fileno())
    if ((current.st_dev, current.st_ino) != (expected.dev, expected.ino)
            or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode)):
        raise Refused("stale", "file was replaced since it was read", path=str(p))
    data = _read_handle(handle, expected.size)
    if len(data) != expected.size or sha256_bytes(data) != expected.sha256:
        details = {"current_sha256": sha256_bytes(data)} if len(data) <= expected.size else {}
        raise Refused("stale", "file changed since it was read", path=str(p), **details)
    handle.seek(0)


def write_contents(p: Path, data: bytes, *, create: bool = False, expected: Version | None = None) -> None:
    """Write a fully prepared edit into the existing file; not an atomic edit.

    `expected` (required unless creating) is the version the edit was prepared
    from: the handle written through must be that file with that content, checked
    immediately before the first byte is written.
    """
    if create:
        replace_atomically(p, data, create=True)
        return
    if expected is None:
        raise ValueError("an existing file is written only against the version read")
    mutation_metadata(p)  # policy: links, streams, extended metadata
    started = False
    try:
        # No O_TRUNC/O_CREAT: a missing file is never silently recreated, and
        # opening the handle does not destroy the old contents.
        fd = os.open(p, os.O_RDWR | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "r+b", buffering=0) as handle:
            _require_version(handle, p, expected)
            started = True
            _overwrite(handle, data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        raise Refused("not_found", "file disappeared before writing", path=str(p)) from None
    except OSError as exc:
        if started:
            raise Failed("write_incomplete",
                         "write or flush failed; contents may be partial; re-read before retrying",
                         path=str(p)) from None
        locked = is_lock_error(exc)
        raise Failed("locked" if locked else "io_error",
                     "file is locked by another process" if locked else str(exc),
                     path=str(p)) from None


def replace_atomically(p: Path, data: bytes, *, create: bool = False, expected: Version | None = None) -> None:
    """Installer publication policy; ordinary file edits use write_contents.

    `expected`: the version the replacement was prepared from, checked again just
    before the replacement is prepared (the rename itself is not conditional).
    """
    tmp_name = None
    backup_folder = None
    retain = False
    metadata = None if create else mutation_metadata(p)
    if expected is not None:
        try:
            with p.open("rb") as handle:
                _require_version(handle, p, expected)
        except FileNotFoundError:
            raise Refused("not_found", "file disappeared before writing", path=str(p)) from None
    if create and os.name == "nt" and ":" in os.path.splitdrive(str(p))[1]:
        raise Refused("forbidden", "writing a named stream directly is unsupported", path=str(p))
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        with os.fdopen(fd, "wb") as handle:
            if metadata is not None:
                if os.name == "nt":
                    from . import windows_files
                    windows_files.prepare(p, Path(tmp_name))
                else:
                    os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
                    os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode))
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if create:
            # link never replaces an existing name on POSIX or NTFS.
            os.link(tmp_name, p)
            os.unlink(tmp_name)
        elif os.name == "nt":
            from . import windows_files
            backup_folder = tempfile.mkdtemp(prefix=".pondnt-original-", dir=str(p.parent))
            windows_files.replace(p, Path(tmp_name), Path(backup_folder) / "original")
        else:
            os.replace(tmp_name, p)
        tmp_name = None
    except FileExistsError:
        raise Refused("stale", "file appeared before publication", path=str(p), current_sha256=None) from None
    except SecurityFinalizationError:
        retain = True
        raise Failed("publication_uncertain",
                     "content published but security finalization failed; inspect retained original",
                     path=str(p), original=backup_folder) from None
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in {1176, 1177}:
            retain = True
            raise Failed("publication_uncertain", "native replacement partly completed; inspect retained candidates",
                         path=str(p), replacement=tmp_name, original=backup_folder) from None
        locked = is_lock_error(exc)
        raise Failed(
            "locked" if locked else "io_error",
            "file is locked by another process" if locked else str(exc),
            path=str(p),
        ) from None
    finally:
        if not retain:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            if backup_folder is not None:
                try:
                    shutil.rmtree(backup_folder)
                except OSError:
                    raise Failed("cleanup_uncertain", "replacement state must be checked; original backup retained",
                                 path=str(p), original=backup_folder) from None
