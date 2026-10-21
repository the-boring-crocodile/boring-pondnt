"""The general services on this one machine, reached with access keys.

setup.ps1 asks; this module prepares. For each wanted service it writes the
service config (loopback HTTP, keys only) next to a key file, makes or keeps
the user's key, and answers with what setup.ps1 still has to do: the
scheduled task's command line and the `.mcp.json` entry.

    python -m boring_pondnt.setup_services --root <release> --state <dir> --index <db>
        [--only sourcecache,knowledge,canvas,core] [--plan] < .mcp.json servers  > plan JSON

The core and the D365 server are set up the same way, with one difference: their tasks run as the
person at logon, not as SYSTEM at boot, because Copilot's commands run in them
and must run as that person (their profile, git credentials, PATH; d365fo.tools elevated).

`--plan` changes nothing. Without it the config and key files are written; a
key is rotated only when the entry in `.mcp.json` does not hold a valid one.
Keys never leave this process except inside the returned entry.

The kit: the core serves `<state>/kit`, not the release's
`kit/`. Setup copies the release's kit there once, when that folder does not
exist, and never writes to it again: a later release only reports which files
differ from its own kit, for the owner to merge deliberately. Uninstall keeps it.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boring_service_kit import keys
from . import disk

LOCAL_DAYS = 365          # renewed by running setup again; the servers listen on 127.0.0.1 only
RENEW_BELOW_DAYS = 30
LABEL = "pondnt setup"


@dataclass(frozen=True)
class Service:
    name: str             # the .mcp.json entry
    short: str            # folder and key issuer suffix
    module: str
    port: int
    account: str = "system"  # "system": SYSTEM at boot; "user": the person at logon

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


SERVICES = {
    "sourcecache": Service("boring-sourcecache", "sourcecache", "boring_sourcecache_d365.server", 18501),
    "knowledge": Service("boring-knowledge", "knowledge", "boring_knowledge.server", 18512),
    "canvas": Service("boring-canvas", "canvas", "boring_canvas.server", 18511),
    "core": Service("boring-pondnt", "core", "boring_pondnt.server", 18513, account="user"),
    # like the core -- the person's account, elevated (d365fo.tools needs administrator rights).
    "d365": Service("boring-d365", "d365", "boring_pondnt.d365_server", 18514, account="user"),
}
DEFAULT_ONLY = "sourcecache,knowledge,canvas,core,d365"
CANVAS_WEB_PORT = 18510
KIT = "kit"


def kit_folder(state: str) -> str:
    return os.path.join(state, KIT)


def arguments(service: Service, folder: str, index: str, state: str) -> list[str]:
    config = os.path.join(folder, "config.json")
    if service.short == "core":
        # Jobs and call logs stay where the stdio core kept them (the person's default state).
        return ["--config", config, "--kit", kit_folder(state)]
    if service.short == "d365":
        return ["serve", "--config", config]
    if service.short == "sourcecache":
        return ["serve", "--config", config, "--db", index, "--state-dir", folder]
    if service.short == "knowledge":
        return ["http", "--config", config, "--db", os.path.join(folder, "notebook.db"), "--role", "global",
                "--state-dir", folder]
    # --unsafe-url-auth: over MCP the canvas link is the only way a person gets the canvas token
    # (create_canvas returns open_url); a Pond's Canvas can run the same way. Here nothing but this
    # machine sees the link: no proxy log, loopback only (found missing on a dev VM).
    return ["--config", config, "--state-dir", os.path.join(folder, "state"), "--host", "127.0.0.1",
            "--port", str(CANVAS_WEB_PORT), "--public-base-url", f"http://127.0.0.1:{CANVAS_WEB_PORT}",
            "--unsafe-url-auth"]


def kit_files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def kit_report(root: str, state: str) -> dict[str, Any]:
    """Where the served kit is and how it compares with this release's kit. Reads only."""
    local, release = Path(kit_folder(state)), Path(root) / KIT
    report: dict[str, Any] = {"folder": str(local), "release": str(release)}
    if not (release / "bootstrap.md").is_file():
        report["state"] = "no-release-kit"
        return report
    if not local.exists():
        report["state"] = "new"
        return report
    if not (local / "bootstrap.md").is_file():
        report["state"] = "no-bootstrap"
        return report
    mine, theirs = kit_files(local), kit_files(release)
    report["differs"] = sorted(n for n in set(mine) | set(theirs) if mine.get(n) != theirs.get(n))
    report["state"] = "kept"
    return report


def seed_kit(root: str, state: str) -> None:
    """Copy the release's kit to an absent state kit folder; an existing folder is never touched."""
    local, release = Path(kit_folder(state)), Path(root) / KIT
    if local.exists():
        return
    local.parent.mkdir(parents=True, exist_ok=True)
    staging = local.parent / f".{KIT}-seeding-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(release, staging)
    # A rename fails when the folder appeared meanwhile: then it is not ours to replace.
    os.rename(staging, local)


def owned(service: Service, entry: Any) -> bool:
    """Ours: the loopback HTTP entry setup writes. Anything else under our name stays for review."""
    return isinstance(entry, dict) and entry.get("type") == "http" and entry.get("url") == service.url


def held_key(entry: Any) -> str | None:
    headers = entry.get("headers") if isinstance(entry, dict) else None
    value = headers.get("Authorization") if isinstance(headers, dict) else None
    if isinstance(value, str) and value.startswith("Bearer " + keys.PREFIX):
        return value[len("Bearer "):]
    return None


def still_good(keyfile: Path, key: str | None, subject: str, now: float) -> bool:
    if key is None or not keyfile.exists():
        return False
    try:
        loaded = keys.KeyFile.load(keyfile)
    except (OSError, ValueError):
        return False
    found = [k for k in loaded.keys if k.sha256 == keys.fingerprint(key)]
    return bool(found) and found[0].subject == subject and found[0].expires > now + RENEW_BELOW_DAYS * keys.DAY


def plan(servers: dict[str, Any], root: str, state: str, index: str, only: list[str],
         user: str, computer: str, apply: bool = False, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    subject = f"{user}@{computer}".lower()
    python = os.path.join(root, "python", "python.exe")
    windowless = os.path.join(root, "python", "pythonw.exe")  # the core's logon task
    result: dict[str, Any] = {"services": [], "conflicts": []}
    if "core" in only:
        report = kit_report(root, state)
        if apply and report["state"] == "new":
            seed_kit(root, state)
            report = kit_report(root, state)
            report["seeded"] = True
        if report["state"] == "no-bootstrap":
            result["conflicts"].append(f"kit: {report['folder']} has no bootstrap.md; "
                                       "left as it is, bootstrap will say it is unreadable")
        result["kit"] = report
    for short in only:
        service = SERVICES[short]
        if short == "sourcecache" and not index:
            result["conflicts"].append("boring-sourcecache: no source index; build it first")
            continue
        current = servers.get(service.name)
        if current is not None and not owned(service, current):
            result["conflicts"].append(f"{service.name}: customized entry retained; not configured")
            continue
        folder = os.path.join(state, "services", service.short)
        local = Path(folder)
        keyfile = local / "keys.json"
        key = held_key(current)
        rotate = not still_good(keyfile, key, subject, now)
        item = {"name": service.name, "url": service.url, "folder": folder, "subject": subject,
                "task": f"pondNT {service.name}", "module": service.module,
                "python": windowless if service.account == "user" else python,
                "account": service.account,
                "arguments": arguments(service, folder, index, state),
                "log": os.path.join(folder, "service.log"),
                "new_key": rotate, "entry_changes": current is None or rotate or current.get("type") != "http"}
        if apply:
            local.mkdir(parents=True, exist_ok=True)
            config = {"url": service.url, "bind": "127.0.0.1", "port": service.port, "keys": {"file": "keys.json"}}
            configfile = local / "config.json"
            disk.replace_atomically(configfile, (json.dumps(config, indent=2) + "\n").encode("utf-8"),
                                    create=not configfile.exists())
            if rotate:
                # Keep the working credential until .mcp.json is durably published.
                # Failed preparation may leave an unused key; successful finalize
                # removes it. Bound repeated interrupted preparations.
                previous = keys.KeyFile.load(keyfile).keys if keyfile.exists() else ()
                if sum(k.subject == subject and k.label == LABEL for k in previous) >= 16:
                    raise ValueError("too many uncommitted setup keys; finish publication or remove obsolete keys")
                issuer = f"key:{computer.lower()}-{service.short}"
                _, key = keys.add(keyfile, subject, "writer", LOCAL_DAYS, LABEL,
                                  issuer=None if keyfile.exists() else issuer, now=now)
            item["entry"] = {"type": "http", "url": service.url, "headers": {"Authorization": f"Bearer {key}"}}
        result["services"].append(item)
    return result


def finalize(servers: dict[str, Any], state: str, user: str, computer: str) -> dict[str, int]:
    """After durable client publication, remove only obsolete setup keys."""
    subject = f"{user}@{computer}".lower()
    removed = 0
    for service in SERVICES.values():
        entry = servers.get(service.name)
        if not owned(service, entry):
            continue
        key = held_key(entry)
        keyfile = Path(state) / "services" / service.short / "keys.json"
        current = keys.KeyFile.load(keyfile)
        held = [k for k in current.keys if key and k.sha256 == keys.fingerprint(key)
                and k.subject == subject and k.label == LABEL and k.expires > time.time()]
        if len(held) != 1:
            raise ValueError("published service credential is not a current setup key; cleanup refused")
        for old in current.keys:
            if old.id != held[0].id and old.subject == subject and old.label == LABEL:
                keys.remove(keyfile, old.id)
                removed += 1
    return {"removed": removed}


def uninstall(servers: dict[str, Any], state: str, root: str = "", index: str = "") -> dict[str, Any]:
    """What -Uninstall removes: our entries, the tasks; the folders (notebook, canvases) are only listed."""
    ours = [s for s in SERVICES.values() if owned(s, servers.get(s.name))]
    return {"removals": [s.name for s in ours],
            "conflicts": [f"{s.name}: customized entry retained" for s in SERVICES.values()
                          if s.name in servers and not owned(s, servers[s.name])],
            "tasks": [f"pondNT {s.name}" for s in ours],
            "folders": [os.path.join(state, "services", s.short) for s in ours],
            "kit": kit_folder(state),
            "services": plan(servers, root, state, index, [s.short for s in ours],
                             getpass.getuser(), platform.node())["services"]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True)
    # %% : argparse formats help strings (Python 3.14 checks them when the argument is added).
    parser.add_argument("--state", required=True, help="e.g. %%LOCALAPPDATA%%\\boring-pondnt")
    parser.add_argument("--index", default="")
    parser.add_argument("--only", default=DEFAULT_ONLY)
    parser.add_argument("--plan", action="store_true", help="look only; write nothing")
    parser.add_argument("--finalize", action="store_true", help="retire obsolete keys after durable client publication")
    parser.add_argument("--uninstall", action="store_true", help="what -Uninstall removes; writes nothing")
    args = parser.parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in only if s not in SERVICES]
    if unknown:
        parser.error(f"unknown services: {', '.join(unknown)}")
    servers = json.load(sys.stdin)
    if not isinstance(servers, dict):
        parser.error("stdin must be the servers object of .mcp.json")
    if sum((args.plan, args.finalize, args.uninstall)) > 1:
        parser.error("plan, finalize and uninstall are mutually exclusive")
    if args.finalize:
        print(json.dumps(finalize(servers, args.state, getpass.getuser(), platform.node())))
        return
    if args.uninstall:
        print(json.dumps(uninstall(servers, args.state, args.root, args.index)))
        return
    answer = plan(servers, args.root, args.state, args.index, only, getpass.getuser(), platform.node(),
                  apply=not args.plan)
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
