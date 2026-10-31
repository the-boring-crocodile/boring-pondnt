"""d365web's browser layer: the installed Edge over the Chrome DevTools Protocol, standard library only.

No Playwright (it runs a Node.js driver; D34) and no WebSocket package: the client side of RFC 6455 is a
handshake and masked frames, and CDP only talks to 127.0.0.1. Everything D365-specific lives above this
module; this one knows browsers, pages, input and the wire.

    browser = Browser.launch(profile_dir, headless=True)
    page = browser.new_page()            # or browser.pages()[0]
    page.navigate("https://host/?cmp=usmf&mi=PurchTableListPage")
    page.evaluate("document.title")
    page.type_text("1001"); page.key("Tab")
    browser.close()
"""

from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
START_SECONDS = 30.0
COMMAND_SECONDS = 30.0


class BrowserError(Exception):
    """A browser-level failure. `code` is stable (no_edge, start_failed, cdp_error, timeout, closed)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


# --- WebSocket client (RFC 6455, client side, loopback only) -------------------------------------

class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        if not url.startswith("ws://"):
            raise BrowserError("cdp_error", f"only ws:// on loopback is supported: {url}")
        hostport, _, path = url[len("ws://"):].partition("/")
        host, _, port = hostport.partition(":")
        if host not in ("127.0.0.1", "localhost"):
            raise BrowserError("cdp_error", "CDP must be on loopback")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("cdp_error", "WebSocket handshake: connection closed")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0]:
            raise BrowserError("cdp_error", f"WebSocket handshake refused: {status[:200]!r}")
        self.buffer = rest
        self.sock.settimeout(None)
        self.send_lock = threading.Lock()

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = secrets.token_bytes(4)
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126); header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127); header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(bytes(header) + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buffer)))
            if not chunk:
                raise BrowserError("closed", "WebSocket closed by the browser")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def receive(self) -> str:
        """The next complete text message; answers pings; raises closed on a close frame."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b2 & 0x80:  # servers must not mask; tolerate it anyway
                mask = self._read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            if opcode == 0x8:
                raise BrowserError("closed", "WebSocket closed by the browser")
            if opcode == 0x9:  # ping -> pong
                with self.send_lock:
                    mask = secrets.token_bytes(4)
                    self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask +
                                      bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            with self.send_lock:
                self.sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- CDP connection --------------------------------------------------------------------------------

class Connection:
    """One WebSocket to the browser; commands by id, events to per-session queues (flat sessions)."""

    def __init__(self, ws_url: str) -> None:
        self.ws = WebSocket(ws_url)
        self.next_id = 0
        self.pending: dict[int, queue.Queue] = {}
        self.listeners: dict[str, list[Callable[[dict], None]]] = {}
        self.lock = threading.Lock()
        self.closed = False
        self.reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self.reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                message = json.loads(self.ws.receive())
                if "id" in message:
                    with self.lock:
                        waiter = self.pending.pop(message["id"], None)
                    if waiter is not None:
                        waiter.put(message)
                else:
                    session = message.get("sessionId", "")
                    for listener in list(self.listeners.get(session, ())):
                        try:
                            listener(message)
                        except Exception:  # a listener must never kill the reader
                            pass
        except (BrowserError, OSError, ValueError):
            pass
        finally:
            self.closed = True
            with self.lock:
                waiters, self.pending = list(self.pending.values()), {}
            for waiter in waiters:
                waiter.put({"error": {"message": "connection closed"}})

    def send(self, method: str, params: dict | None = None, session: str = "",
             timeout: float = COMMAND_SECONDS) -> dict:
        if self.closed:
            raise BrowserError("closed", "browser connection is closed")
        waiter: queue.Queue = queue.Queue(1)
        with self.lock:
            self.next_id += 1
            ident = self.next_id
            self.pending[ident] = waiter
        message: dict[str, Any] = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        try:
            answer = waiter.get(timeout=timeout)
        except queue.Empty:
            with self.lock:
                self.pending.pop(ident, None)
            raise BrowserError("timeout", f"{method}: no answer within {timeout:.0f} s") from None
        if "error" in answer:
            raise BrowserError("cdp_error", f"{method}: {answer['error'].get('message', answer['error'])}")
        return answer.get("result", {})

    def listen(self, session: str, listener: Callable[[dict], None]) -> None:
        self.listeners.setdefault(session, []).append(listener)

    def close(self) -> None:
        self.closed = True
        self.ws.close()


# --- Page ------------------------------------------------------------------------------------------

KEYS = {  # key -> (code, windowsVirtualKeyCode)
    "Tab": ("Tab", 9), "Enter": ("Enter", 13), "Escape": ("Escape", 27), "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46), "ArrowDown": ("ArrowDown", 40), "ArrowUp": ("ArrowUp", 38),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowRight": ("ArrowRight", 39), "Home": ("Home", 36), "End": ("End", 35),
    "PageDown": ("PageDown", 34), "PageUp": ("PageUp", 33), "Space": ("Space", 32), "Insert": ("Insert", 45),
    **{f"F{n}": (f"F{n}", 111 + n) for n in range(1, 13)},
}
MODIFIERS = {"Alt": 1, "Control": 2, "Ctrl": 2, "Meta": 4, "Shift": 8}


class Page:
    """One tab, attached as a flat session. Tracks in-flight network requests for the busy-wait."""

    def __init__(self, conn: Connection, target_id: str) -> None:
        self.conn, self.target_id = conn, target_id
        self.session = conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        self.inflight: set[str] = set()
        self.last_network = time.monotonic()
        self.dialogs: list[dict] = []
        self.events_lock = threading.Lock()
        conn.listen(self.session, self._on_event)
        for domain in ("Page.enable", "Runtime.enable", "Network.enable"):
            self.send(domain)

    def send(self, method: str, params: dict | None = None, timeout: float = COMMAND_SECONDS) -> dict:
        return self.conn.send(method, params, session=self.session, timeout=timeout)

    def _on_event(self, message: dict) -> None:
        method, params = message.get("method", ""), message.get("params", {})
        with self.events_lock:
            if method == "Network.requestWillBeSent":
                self.inflight.add(params.get("requestId", ""))
                self.last_network = time.monotonic()
            elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                self.inflight.discard(params.get("requestId", ""))
                self.last_network = time.monotonic()
            elif method == "Page.javascriptDialogOpening":
                self.dialogs.append(params)

    def network_idle_for(self) -> float:
        """Seconds since the last network event while nothing is in flight; 0 while requests run."""
        with self.events_lock:
            return 0.0 if self.inflight else time.monotonic() - self.last_network

    # navigation and script
    def navigate(self, url: str, timeout: float = COMMAND_SECONDS) -> None:
        result = self.send("Page.navigate", {"url": url}, timeout=timeout)
        if result.get("errorText"):
            raise BrowserError("cdp_error", f"navigate: {result['errorText']}")

    def evaluate(self, expression: str, timeout: float = COMMAND_SECONDS, await_promise: bool = False) -> Any:
        result = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": await_promise}, timeout=timeout)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "")
            raise BrowserError("cdp_error", f"script error: {text[:500]}")
        return result.get("result", {}).get("value")

    def url(self) -> str:
        return self.evaluate("location.href") or ""

    # input, like a person
    def click(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                   "clickCount": 1 if kind != "mouseMoved" else 0})

    def move(self, x: float, y: float) -> None:
        """The mouse moves to (x, y) without a click (leaves a hover preview, as a person would)."""
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none", "clickCount": 0})

    def type_text(self, text: str) -> None:
        """Typed character by character as key events with text (fires the page's key handlers)."""
        for char in text:
            self.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": char, "unmodifiedText": char, "key": char})
            self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})

    def key(self, combo: str) -> None:
        """A key or chord: 'Tab', 'Enter', 'Escape', 'Alt+N', 'Ctrl+S', 'Shift+Tab'."""
        *mods, name = combo.split("+")
        modifiers = 0
        for mod in mods:
            if mod not in MODIFIERS:
                raise BrowserError("cdp_error", f"unknown modifier {mod!r}")
            modifiers |= MODIFIERS[mod]
        if name in KEYS:
            code, vk = KEYS[name]
            key = " " if name == "Space" else name
        elif len(name) == 1:
            key, code, vk = name, ("Key" + name.upper()) if name.isalpha() else name, ord(name.upper())
        else:
            raise BrowserError("cdp_error", f"unknown key {name!r}")
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        self.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def screenshot(self, path: Path) -> Path:
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        path.write_bytes(base64.b64decode(data))
        return path

    def accessibility_tree(self) -> list[dict]:
        return self.send("Accessibility.getFullAXTree", {}, timeout=60)["nodes"]


# --- Browser ---------------------------------------------------------------------------------------

def find_edge() -> str:
    for path in EDGE_PATHS:
        if os.path.isfile(path):
            return path
    raise BrowserError("no_edge", "Microsoft Edge is not installed in its default location")


# A visible Edge whose window nobody sees -- covered by other windows, or in a locked or
# disconnected session, like a test box's auto-logon session -- counts as occluded:
# Chromium stops rendering it like a background tab, and D365's page then ignores clicks and
# keys after a navigation until a frame is forced (a screenshot did it). Observed 2026-10-30 on a
# Windows test box with rc39 and 0.2.7: first click on New after web_open failed; with either
# CalculateNativeWinOcclusion disabled or the three background switches it worked 3/3; headless
# was never affected. Both are applied: occlusion detection is what marks the window hidden
# (Chromium docs, "Windows Native Window Occlusion Detection"), the switches keep the renderer
# from being backgrounded anyway (BigBinary, "Debug failing puppeteer tests due to background
# tab": the three together fixed headful tests out of view). Chromium honours only the last
# --disable-features, so there is one list.
FEATURES_OFF = ("msEdgeSidebarV2", "msUndersideButton")
VISIBLE_FEATURES_OFF = ("CalculateNativeWinOcclusion",)
VISIBLE_SWITCHES = ("--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding")


def launch_args(profile_dir: Path, port: int, headless: bool) -> list[str]:
    """Edge's arguments for d365web (without the executable, extra arguments and start page)."""
    features = FEATURES_OFF if headless else FEATURES_OFF + VISIBLE_FEATURES_OFF
    args = [f"--user-data-dir={profile_dir}", f"--remote-debugging-port={int(port)}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--disable-features=" + ",".join(features), "--window-size=1600,1000"]
    args += ["--headless=new"] if headless else list(VISIBLE_SWITCHES)
    return args


@dataclass
class Browser:
    process: subprocess.Popen | None
    conn: Connection
    profile_dir: Path

    @classmethod
    def launch(cls, profile_dir: Path, headless: bool = True, edge: str | None = None,
               extra_args: tuple[str, ...] = (), port: int = 0) -> "Browser":
        """Start Edge with its own profile and a loopback debugging port (0 = random)."""
        profile_dir = Path(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        port_file = profile_dir / "DevToolsActivePort"
        try:
            port_file.unlink()
        except FileNotFoundError:
            pass
        args = [edge or find_edge(), *launch_args(profile_dir, port, headless), *extra_args, "about:blank"]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserError("start_failed", f"Edge exited with {process.returncode} (profile in use?)")
            try:
                lines = port_file.read_text(encoding="utf-8").split()
                if len(lines) >= 2:
                    ws = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                    return cls(process, Connection(ws), profile_dir)
            except (FileNotFoundError, OSError):
                pass
            if port:  # Edge 155 (2026-10-23) no longer writes DevToolsActivePort for a fixed port: ask the port
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=1) as response:
                        return cls(process, Connection(json.loads(response.read())["webSocketDebuggerUrl"]), profile_dir)
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(0.2)
        process.kill()
        raise BrowserError("start_failed", f"no debugging port within {START_SECONDS:.0f} s")

    @classmethod
    def attach(cls, port: int, profile_dir: Path) -> "Browser":
        """Attach to an Edge already listening on a loopback debugging port (e.g. a headed one Wald uses)."""
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
            ws = json.loads(response.read())["webSocketDebuggerUrl"]
        return cls(None, Connection(ws), Path(profile_dir))

    def version(self) -> dict:
        return self.conn.send("Browser.getVersion")

    def pages(self) -> list[Page]:
        targets = self.conn.send("Target.getTargets")["targetInfos"]
        return [Page(self.conn, t["targetId"]) for t in targets if t["type"] == "page"]

    def new_page(self, url: str = "about:blank") -> Page:
        target = self.conn.send("Target.createTarget", {"url": url})["targetId"]
        return Page(self.conn, target)

    def close(self) -> None:
        try:
            self.conn.send("Browser.close", timeout=5)
        except BrowserError:
            pass
        self.conn.close()
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
