"""Shared client-credentials token and HTTP transport for D365 services."""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TOKEN_TIMEOUT = 30.0


class EntraError(Exception):
    """A token or secret failure with its stable code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _token_http_error(response: Response) -> EntraError:
    message = f"Entra token request: HTTP {response.status}"
    details: dict[str, object] = {}
    try:
        data = json.loads(response.text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", error):
            details["error"] = error
            message += ": " + error
        codes = data.get("error_codes")
        if isinstance(codes, list):
            details["error_codes"] = [code for code in codes if type(code) is int and 0 <= code <= 999_999_999][:16]
        description = data.get("error_description")
        if isinstance(description, str) and description:
            first_line = description.splitlines()[0]
            first_line = re.split(r"\b(?:trace[ _]id|correlation[ _]id|timestamp):", first_line,
                                  maxsplit=1, flags=re.IGNORECASE)[0].strip()
            first_line = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
                                "[ID omitted]", first_line, flags=re.IGNORECASE)
            if first_line:
                message += ": " + first_line[:300]
    return EntraError("token_failed", message, **details)


def read_dpapi_secret(path: str) -> str:
    """A secret written by `ConvertFrom-SecureString` (DPAPI, current user): hex of the blob."""
    if sys.platform != "win32":
        raise EntraError("secret_unavailable", "DPAPI secrets can only be read on Windows")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    try:
        raw = bytes.fromhex(Path(path).read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError) as exc:
        raise EntraError("secret_unavailable", f"cannot read the client secret file: {exc}") from None
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in, blob_out = Blob(len(raw), buffer), Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise EntraError("secret_unavailable", "DPAPI could not decrypt the client secret (other user or machine?)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-16-le")
    finally:
        kernel32.LocalFree(blob_out.pbData)


@dataclass
class Response:
    status: int
    text: str
    truncated: bool = False
    raw: bytes = b""


Transport = Callable[..., Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward authenticated D365 requests to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


FRESH_TOKEN_SECONDS = 60.0  # a 401 for a token this young is not answered with a new token


def urllib_transport(url: str, headers: dict[str, str], body: bytes | None, timeout: float,
                     method: str = "POST", max_bytes: int | None = None,
                     allow_redirects: bool = False, binary: bool = False) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    def read(stream: object) -> tuple[str, bool, bytes]:
        raw = stream.read(max_bytes + 1) if max_bytes is not None else stream.read()  # type: ignore[attr-defined]
        truncated = max_bytes is not None and len(raw) > max_bytes
        data = raw[:max_bytes]
        return ("" if binary else data.decode("utf-8", "replace")), truncated, data if binary else b""
    try:
        opener = urllib.request.urlopen if allow_redirects else urllib.request.build_opener(_NoRedirect()).open
        with opener(request, timeout=timeout) as response:
            text, truncated, raw = read(response)
            return Response(response.status, text, truncated, raw)
    except urllib.error.HTTPError as exc:
        with exc:
            text, truncated, raw = read(exc)
        return Response(exc.code, text, truncated, raw)


class TokenSource:
    def __init__(self, tenant: str, client_id: str, scope: str,
                 transport: Transport, secret: Callable[[], str]) -> None:
        self.tenant, self.client_id, self.scope = tenant, client_id, scope
        self.transport, self.secret = transport, secret
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def get(self, *, deadline: float | None = None) -> str:
        def remaining() -> float:
            seconds = deadline - time.monotonic() if deadline is not None else math.inf
            if seconds <= 0:
                raise EntraError("timeout", "the call deadline expired while obtaining an Entra token")
            return seconds

        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=min(remaining(), threading.TIMEOUT_MAX)):
            raise EntraError("timeout", "the call deadline expired while obtaining an Entra token")
        try:
            remaining()
            if self._token and time.monotonic() < self._expires - 120:
                return self._token
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.secret(), "scope": self.scope}).encode()
            url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
            try:
                response = self.transport(url, {"Content-Type": "application/x-www-form-urlencoded"},
                                          body, min(TOKEN_TIMEOUT, remaining()))
            except (OSError, socket.timeout) as exc:
                raise EntraError("token_failed", f"Entra token request failed: {exc}") from None
            except (http.client.HTTPException, ValueError):
                raise EntraError("token_failed", "Entra token transport rejected the request or response") from None
            if response.status != 200:
                raise _token_http_error(response)
            try:
                data = json.loads(response.text)
                token = data.get("access_token") if isinstance(data, dict) else None
                if not isinstance(token, str) or not token.strip():
                    raise ValueError
                lifetime = data.get("expires_in", 3600)
                if isinstance(lifetime, bool):
                    raise ValueError
                seconds = float(lifetime)
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise EntraError("token_failed", "Entra returned an unusable token reply") from None
            self._token = token
            self._expires = time.monotonic() + seconds
            self._fetched = time.monotonic()
            return token
        finally:
            self._lock.release()

    def invalidate(self, token: str) -> bool:
        """Forget `token`, which the service rejected (HTTP 401), so the next get() asks Entra.

        Only that token: another thread may already have replaced it with a fresh one. Entra can
        revoke a token before its expiry (secret rotation, app changes) and the cache would
        otherwise serve it until then."""
        with self._lock:
            # A token Entra issued moments ago and the service rejects is a setup problem (the app is
            # not registered in D365), not a revocation: keep it, so callers do not hammer Entra.
            if self._token == token and time.monotonic() - getattr(self, "_fetched", 0.0) >= FRESH_TOKEN_SECONDS:
                self._token, self._expires = "", 0.0
                return True
            return False
