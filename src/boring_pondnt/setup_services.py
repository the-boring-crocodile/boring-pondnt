"""The general services on this one machine, reached with access keys.

setup.ps1 asks; this module prepares. For each wanted service it writes the
service config (loopback HTTP, keys only) next to a key file, makes or keeps
the user's key, and answers with what setup.ps1 still has to do: the
scheduled task's command line and the `.mcp.json` entry.

    python -m boring_pondnt.setup_services --root <release> --state <dir> --index <db>
        [--only sourcecache,knowledge,canvas,core] [--gateway keep|none|all|d365,...] [--plan]
        < .mcp.json servers  > plan JSON

The core and the D365 server are set up the same way, with one difference: their tasks run as the
person at logon, not as SYSTEM at boot, because Copilot's commands run in them
and must run as that person (their profile, git credentials, PATH; d365fo.tools elevated).

`--plan` changes nothing. Without it the config and key files are written; a
key is rotated only when the entry in `.mcp.json` does not hold a valid one.
Keys never leave this process except inside the returned entry.

The gateway (optional, DECISIONS D41): `--gateway` puts chosen services behind one
`.mcp.json` entry, boring-gateway, a boring-mcp-ferry with four tools (service_list,
service_describe, service_read, service_call) instead of every service's own. A
gated service keeps its task, port and key; the key goes into a headers file in the
gateway's folder instead of an `.mcp.json` entry, and its direct entry is removed.
The core and Knowledge always stay direct. `keep` (the default) keeps whatever is
behind the gateway now; `none` takes everything back out and retires the gateway.

The kit: the core serves `<state>/handbook`, not the release's
`handbook/`. Setup copies the release's kit there once, when that folder does not
exist, and never writes to it again: a later release only reports which files
differ from its own kit, for the owner to merge deliberately. Uninstall keeps it.
An old install's `<state>/kit` (before the handbook) is not migrated: setup refuses
and says how to reinstall (Wald, 2026-10-30).
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
    # Opt-in (not in DEFAULT_ONLY): needs an Entra app and d365whsapp.json in its folder (docs/D365WHSAPP.md).
    # The person's account: the client secret is DPAPI-protected for them.
    "d365whsapp": Service("boring-d365whsapp", "d365whsapp", "boring_pondnt.d365whsapp_server", 18515, account="user"),
    # Opt-in: drives a signed-in Edge profile of the person (docs/D365WEB.md), so the person's account too.
    "d365web": Service("boring-d365web", "d365web", "boring_pondnt.d365web_server", 18516, account="user"),
    # Only with --gateway: never in --only. SYSTEM at boot like the services; it calls them on loopback.
    "gateway": Service("boring-gateway", "gateway", "boring_mcp_ferry.server", 18517),
}
GATEWAY = "gateway"
NEVER_GATED = ("core", "knowledge", GATEWAY)  # the core and the notebook stay Copilot's own tools
GATEWAY_NAME = "pondnt"                        # the ferry's name for its gateway
FERRY = "ferry.json"
GUIDES = {"d365": "d365.md", "d365whsapp": "warehouse.md"}  # kit pages service_describe returns
DEFAULT_ONLY = "sourcecache,knowledge,canvas,core,d365"
WHSAPP_CONFIG = "d365whsapp.json"
OPT_IN_CONFIGS = {"d365whsapp": (WHSAPP_CONFIG, "docs/D365WHSAPP.md"), "d365web": ("d365web.json", "docs/D365WEB.md")}
CANVAS_WEB_PORT = 18510
KIT = "handbook"


def kit_folder(state: str) -> str:
    return os.path.join(state, KIT)


def arguments(service: Service, folder: str, index: str, state: str) -> list[str]:
    config = os.path.join(folder, "config.json")
    if service.short == "core":
        # Jobs and call logs stay where the stdio core kept them (the person's default state).
        return ["--config", config, "--handbook", kit_folder(state)]
    if service.short == "d365":
        return ["serve", "--config", config]
    if service.short == GATEWAY:
        return ["http", "--config", os.path.join(folder, FERRY), "--state-dir", folder]
    if service.short in OPT_IN_CONFIGS:
        return ["serve", "--config", config, f"--{service.short}", os.path.join(folder, OPT_IN_CONFIGS[service.short][0])]
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


def gateway_folder(state: str) -> Path:
    return Path(state) / "services" / GATEWAY


def headers_file(state: str, short: str) -> Path:
    return gateway_folder(state) / f"{short}.headers.json"


def gated_now(state: str) -> list[str]:
    """The services behind the gateway as installed (its ferry.json); none when there is none."""
    try:
        data = json.loads((gateway_folder(state) / FERRY).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    backends = data.get("backends") if isinstance(data, dict) else None
    if not isinstance(backends, dict) or any(s not in SERVICES or s in NEVER_GATED for s in backends):
        raise ValueError("Invalid installed gateway configuration; inspect ferry.json before changing setup")
    return list(backends)


def choose_gated(choice: str, only: list[str], state: str) -> tuple[list[str], list[str]]:
    """(services to set up, those of them behind the gateway) for --gateway keep|none|all|a,b."""
    value = choice.strip().lower()
    kept = gated_now(state)
    # Previously gated services must return as direct entries when they leave the gateway,
    # including opt-in services omitted from this invocation of --only.
    configured = only + [s for s in kept if s not in only]
    if value in ("", "keep"):
        return configured, kept
    if value == "none":
        return configured, []
    if value == "all":
        return configured, [s for s in only if s not in NEVER_GATED]
    wanted = [s.strip() for s in value.split(",") if s.strip()]
    wrong = [s for s in wanted if s not in only or s in NEVER_GATED]
    if wrong:
        raise ValueError(f"--gateway: {', '.join(wrong)} not among the chosen services, or never behind the "
                         "gateway (the core and Knowledge stay direct)")
    return configured, [s for s in only if s in wanted]


def header_key(state: str, short: str) -> str | None:
    try:
        data = json.loads(headers_file(state, short).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return held_key({"headers": data}) if isinstance(data, dict) else None


def write_private(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    disk.replace_atomically(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), create=not path.exists())


def ferry_config(state: str, carried: list[str]) -> dict[str, Any]:
    """The gateway's ferry.json: each gated service on loopback with its key, its kit page as guide."""
    backends: dict[str, Any] = {}
    for short in carried:
        backend: dict[str, Any] = {"upstream": {"url": SERVICES[short].url, "headers_file": f"{short}.headers.json"}}
        guide = Path(kit_folder(state)) / GUIDES.get(short, "")
        if short in GUIDES and guide.is_file():
            backend["guide_file"] = str(guide)
        backends[short] = backend
    return {"backends": backends, "gateways": {GATEWAY_NAME: {"service": "config.json", "backends": carried}}}


def kit_files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def kit_report(root: str, state: str) -> dict[str, Any]:
    """Where the served kit is and how it compares with this release's kit. Reads only."""
    local, release = Path(kit_folder(state)), Path(root) / KIT
    legacy = Path(state) / "kit"
    report: dict[str, Any] = {"folder": str(local), "release": str(release)}
    if os.path.lexists(legacy):
        report["state"] = "old-kit"
        report["legacy"] = str(legacy)
        return report
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


def describe(service: Service, root: str, state: str, index: str, subject: str) -> dict[str, Any]:
    folder = os.path.join(state, "services", service.short)
    python = os.path.join(root, "python", "python.exe")
    windowless = os.path.join(root, "python", "pythonw.exe")  # the logon tasks'
    return {"name": service.name, "url": service.url, "folder": folder, "subject": subject,
            "task": f"pondNT {service.name}", "module": service.module,
            "python": windowless if service.account == "user" else python,
            "account": service.account,
            "arguments": arguments(service, folder, index, state),
            "log": os.path.join(folder, "service.log")}


def plan(servers: dict[str, Any], root: str, state: str, index: str, only: list[str],
         user: str, computer: str, apply: bool = False, now: float | None = None,
         gated: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    now = time.time() if now is None else now
    subject = f"{user}@{computer}".lower()
    result: dict[str, Any] = {"services": [], "conflicts": [], "removals": [], "retire": []}
    gated = [s for s in gated if s in only and s not in NEVER_GATED]
    carried: list[str] = []
    gateway = SERVICES[GATEWAY]
    gateway_entry = servers.get(gateway.name)
    if gated and gateway_entry is not None and not owned(gateway, gateway_entry):
        # Refuse before publishing headers or removing a direct entry: this gateway is not ours.
        result["conflicts"].append(f"{gateway.name}: customized entry retained; gateway transition not configured")
        return result
    if "core" in only:
        report = kit_report(root, state)
        if report["state"] == "old-kit":
            result["conflicts"].append(
                f"handbook: {report['legacy']} is an install from before the handbook, which this release does "
                "not migrate; nothing configured. Uninstall it with its own setup (-Uninstall), move that "
                "folder aside, then run this setup")
            result["handbook"] = report
            return result
        if apply and report["state"] == "new":
            seed_kit(root, state)
            report = kit_report(root, state)
            report["seeded"] = True
        if report["state"] == "no-bootstrap":
            result["conflicts"].append(f"handbook: {report['folder']} has no bootstrap.md; "
                                       "left as it is, bootstrap will say it is unreadable")
        result["handbook"] = report
    for short in [s for s in only if s != GATEWAY] + ([GATEWAY] if gated else []):
        service = SERVICES[short]
        behind = short in gated
        if short == GATEWAY and not carried:
            result["conflicts"].append(f"{service.name}: nothing to carry; not configured")
            continue
        if short == "sourcecache" and not index:
            result["conflicts"].append("boring-sourcecache: no source index; build it first")
            continue
        if short in OPT_IN_CONFIGS and not (Path(state) / "services" / service.short / OPT_IN_CONFIGS[short][0]).is_file():
            name, doc = OPT_IN_CONFIGS[short]
            result["conflicts"].append(f"{service.name}: no {name} in "
                                       f"{os.path.join(state, 'services', service.short)}; see {doc}")
            continue
        current = servers.get(service.name)
        if current is not None and not owned(service, current):
            result["conflicts"].append(f"{service.name}: customized entry retained; not configured")
            continue
        item = describe(service, root, state, index, subject)
        local = Path(item["folder"])
        keyfile = local / "keys.json"
        key = header_key(state, short) if behind else held_key(current)
        rotate = not still_good(keyfile, key, subject, now)
        item.update(new_key=rotate, behind=gateway.name if behind else None,
                    entry_changes=(current is not None) if behind
                    else (current is None or rotate or current.get("type") != "http"))
        if behind:
            carried.append(short)
            if current is not None:
                result["removals"].append(service.name)  # reached through the gateway from now on
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
            if behind:
                write_private(headers_file(state, short), {"Authorization": f"Bearer {key}"})
            else:
                item["entry"] = {"type": "http", "url": service.url, "headers": {"Authorization": f"Bearer {key}"}}
            if short == GATEWAY:
                write_private(local / FERRY, ferry_config(state, carried))
        result["services"].append(item)
    if carried:
        result["gateway"] = {"name": gateway.name, "carries": [SERVICES[s].name for s in carried]}
    if not gated and (owned(gateway, servers.get(gateway.name)) or gated_now(state)):
        # -Gateway none: the gateway goes; its services come back as entries of their own (above).
        if owned(gateway, servers.get(gateway.name)):
            result["removals"].append(gateway.name)
        result["retire"].append(describe(gateway, root, state, index, subject))
    # Keep old routing files through preparation and service startup. An interrupted
    # transition must retain the previous gateway's inputs until client publication.
    return result


def finalize(servers: dict[str, Any], state: str, user: str, computer: str) -> dict[str, int]:
    """After durable client publication (and the gateway's restart), remove only obsolete setup keys."""
    subject = f"{user}@{computer}".lower()
    removed = 0
    gateway_entry = servers.get(SERVICES[GATEWAY].name)
    configured = gated_now(state)
    if configured and gateway_entry is not None and not owned(SERVICES[GATEWAY], gateway_entry):
        raise ValueError("gateway entry is customized; routing and key cleanup refused")
    behind = configured if owned(SERVICES[GATEWAY], gateway_entry) else []
    for short, service in SERVICES.items():
        if short in behind:
            key = header_key(state, short)
            if key is None:
                continue
        else:
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
    # Only the durably published client determines whether the old gateway is retired.
    # Running setup again can still read its old backend roster before this point.
    for short in SERVICES:
        if short not in behind:
            headers_file(state, short).unlink(missing_ok=True)
    if not behind and gateway_entry is None:
        (gateway_folder(state) / FERRY).unlink(missing_ok=True)
    return {"removed": removed}


def uninstall(servers: dict[str, Any], state: str, root: str = "", index: str = "") -> dict[str, Any]:
    """What -Uninstall removes: our entries, the tasks; the folders (notebook, canvases) are only listed."""
    behind = gated_now(state)
    ours = [s for k, s in SERVICES.items()
            if owned(s, servers.get(s.name)) or k in behind or (k == GATEWAY and behind)]
    found = plan(servers, root, state, index, [s.short for s in ours if s.short != GATEWAY],
                 getpass.getuser(), platform.node(), gated=behind)
    return {"removals": [s.name for s in ours if owned(s, servers.get(s.name))],
            "conflicts": [f"{s.name}: customized entry retained" for s in SERVICES.values()
                          if s.name in servers and not owned(s, servers[s.name])],
            "tasks": [f"pondNT {s.name}" for s in ours],
            "folders": [os.path.join(state, "services", s.short) for s in ours],
            "handbook": kit_folder(state),
            "services": [*found["services"], *found["retire"]]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True)
    # %% : argparse formats help strings (Python 3.14 checks them when the argument is added).
    parser.add_argument("--state", required=True, help="e.g. %%LOCALAPPDATA%%\\boring-pondnt")
    parser.add_argument("--index", default="")
    parser.add_argument("--only", default=DEFAULT_ONLY)
    parser.add_argument("--gateway", default="keep", help="keep, none, all, or services behind boring-gateway")
    parser.add_argument("--plan", action="store_true", help="look only; write nothing")
    parser.add_argument("--finalize", action="store_true", help="retire obsolete keys after durable client publication")
    parser.add_argument("--uninstall", action="store_true", help="what -Uninstall removes; writes nothing")
    args = parser.parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in only if s not in SERVICES or s == GATEWAY]
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
    try:
        only, gated = choose_gated(args.gateway, only, args.state)
    except ValueError as error:
        parser.error(str(error))
    answer = plan(servers, args.root, args.state, args.index, only, getpass.getuser(), platform.node(),
                  apply=not args.plan, gated=gated)
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
