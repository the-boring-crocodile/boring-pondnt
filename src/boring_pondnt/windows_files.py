"""Native replacement metadata; no ignore-ACL flags or destructive fallback.

ReplaceFileW preserves streams but can rewrite DACL inheritance. Snapshot security,
restore only a changed DACL, then verify before discarding the original backup. Special links are refused by disk. Keep both candidates
on documented partial failure so an operator can reconcile them.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path

PARTIAL_REPLACEMENT_ERRORS = {1176, 1177}


def _library(name):
    return ctypes.WinDLL(name, use_last_error=True)


def _function(library, name, arguments):
    fn = getattr(library, name)
    fn.argtypes = arguments
    fn.restype = wintypes.BOOL
    return fn


def _path(path):
    value = str(Path(path).absolute())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


class SecurityFinalizationError(OSError):
    """Content was published, but security verification did not finish."""


def _security_descriptor(source: Path):
    security = _library("advapi32")
    get = _function(security, "GetFileSecurityW",
                    [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)])
    size = wintypes.DWORD()
    get(_path(source), 7, None, 0, ctypes.byref(size))  # OWNER | GROUP | DACL
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(size.value)
    if not get(_path(source), 7, descriptor, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor


def _security_text(descriptor):
    convert = _function(_library("advapi32"),
                        "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                        [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)])
    text = ctypes.c_void_p()
    if not convert(descriptor, 1, 7, ctypes.byref(text), None):
        raise ctypes.WinError(ctypes.get_last_error())
    free = _library("kernel32").LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.wstring_at(text.value)
    finally:
        free(text)


def _set_security(path: Path, information: int, descriptor):
    set_security = _function(_library("advapi32"), "SetFileSecurityW",
                             [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p])
    if not set_security(_path(path), information, descriptor):
        raise ctypes.WinError(ctypes.get_last_error())


def prepare(source: Path, replacement: Path):
    """Copy security onto the empty staging file before writing content."""
    _set_security(replacement, 7, _security_descriptor(source))
    kernel = _library("kernel32")
    get_attributes = kernel.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attributes = get_attributes(_path(source))
    if attributes == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    set_attributes = _function(kernel, "SetFileAttributesW", [wintypes.LPCWSTR, wintypes.DWORD])
    # Other supported native properties (compression/encryption) are merged by ReplaceFile.
    settable = attributes & (0x1 | 0x2 | 0x4 | 0x20 | 0x100 | 0x1000 | 0x2000)
    if not set_attributes(_path(replacement), settable or 0x80):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_native(source: Path, replacement: Path, backup: Path):
    fn = _function(_library("kernel32"), "ReplaceFileW",
                   [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p])
    if not fn(_path(source), _path(replacement), _path(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def replace(source: Path, replacement: Path, backup: Path):
    saved = _security_descriptor(source)
    before = _security_text(saved)
    _replace_native(source, replacement, backup)
    try:
        if _security_text(_security_descriptor(source)) != before:
            # Unconditional restoration strips AI from an already-correct
            # protected DACL. Leave an exact native result alone.
            _set_security(source, 4, saved)
            if _security_text(_security_descriptor(source)) != before:
                raise OSError("security descriptor differs after restoration")
    except OSError as exc:
        raise SecurityFinalizationError("security finalization failed after publication") from exc
