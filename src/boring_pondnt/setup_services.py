"""Prepare normal desktop services, preserving operator settings and credentials.

Create config.json once, with a one-time legacy keys upgrade.
Generate the fixed gateway from the owned task scope.
Each service owns client.headers.json, used by both .mcp.json and Ferry.
Reruns repair generated output with the same bearer; only --rotate-keys
replaces it. Uninstall retains configuration, keys, data and the handbook.
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
from urllib.parse import urlsplit

from boring_service_kit import keys
from boring_service_kit.filelock import file_lock
from . import disk

LOCAL_DAYS = 36525        # fixed 100-year technical expiry; no automatic renewal
LABEL = "pondnt setup"


@dataclass(frozen=True)
class Service:
    name: str             # the .mcp.json entry
    short: str            # folder and key issuer suffix
    module: str
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


SERVICES = {
    "sourcecache": Service("boring-sourcecache", "sourcecache", "boring_sourcecache_d365.server", 18501),
    "knowledge": Service("boring-knowledge", "knowledge", "boring_knowledge.server", 18512),
    "canvas": Service("boring-canvas", "canvas", "boring_canvas.server", 18511),
    "core": Service("boring-pondnt", "core", "boring_pondnt.server", 18513),
    # like the core -- the person's account, elevated (d365fo.tools needs administrator rights).
    "d365": Service("boring-d365", "d365", "boring_pondnt.d365_server", 18514),
    # Opt-in (not in DEFAULT_ONLY): needs an Entra app and d365whsapp.json in its folder (docs/D365WHSAPP.md).
    # The person's account: the client secret is DPAPI-protected for them.
    "d365whsapp": Service("boring-d365whsapp", "d365whsapp", "boring_pondnt.d365whsapp_server", 18515),
    # Opt-in: drives a signed-in Edge profile of the person (docs/D365WEB.md), so the person's account too.
    "d365web": Service("boring-d365web", "d365web", "boring_pondnt.d365web_server", 18516),
    # Opt-in: its own Entra app; the secret is DPAPI-protected for the person (docs/D365DATA.md).
    "d365data": Service("boring-d365data", "d365data", "boring_pondnt.d365data_server", 18518),
    # Only with the gateway selected: it calls the other services on loopback.
    "gateway": Service("boring-gateway", "gateway", "boring_mcp_ferry.server", 18517),
}
GATEWAY = "gateway"
NEVER_GATED = ("core", "knowledge", GATEWAY)  # the core and the notebook stay Copilot's own tools
GATEWAY_NAME = "pondnt"                        # the ferry's name for its gateway
FERRY = "ferry.json"
GUIDES = {"d365": "d365.md", "d365whsapp": "warehouse.md", "d365data": "data.md"}  # kit pages service_describe returns
DEFAULT_ONLY = "sourcecache,knowledge,canvas,core,d365"
WHSAPP_CONFIG = "d365whsapp.json"
OPT_IN_CONFIGS = {"d365whsapp": (WHSAPP_CONFIG, "docs/D365WHSAPP.md"),
                  "d365web": ("d365web.json", "docs/D365WEB.md"),
                  "d365data": ("d365data.json", "docs/D365DATA.md")}
KIT = "handbook"


def read_settings(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: cannot read settings ({type(error).__name__})") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def service_settings(service: Service, state: str, *,
                     migrations: dict[Path, tuple[dict[str, Any], disk.Version]] | None = None) -> dict[str, Any]:
    path = Path(state) / "services" / service.short / "config.json"
    if not path.exists():
        return {"url": service.url, "bind": "127.0.0.1", "port": service.port,
                "mode": "keys", "keys": {"file": "keys.json"}}
    value = read_settings(path)
    from boring_service_kit.http import ServiceConfig
    legacy = (set(value) == {"url", "bind", "port", "keys"}
              and isinstance(value["keys"], dict) and set(value["keys"]) == {"file"})
    if legacy and migrations is not None:
        raw, source = disk.read_version(path, 512 * 1024)
        if json.loads(raw.decode("utf-8-sig")) != value:
            raise ValueError(f"{path}: changed during setup; inspect and retry")
    try:
        if legacy:
            value["mode"] = "keys"
            info = ServiceConfig.inspect_value(value, path)
        else:
            info = ServiceConfig.inspect(path)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from None
    url = urlsplit(info.url)
    mcp_path = url.path.endswith("/mcp") if service.short == "canvas" else url.path == "/mcp"
    if not (info.bind == "127.0.0.1" and url.scheme == "http"
            and url.hostname == "127.0.0.1" and url.port == info.port and mcp_path):
        raise ValueError(f"{path}: use a loopback HTTP /mcp URL and matching port (edit both together)")
    if legacy and migrations is not None:
        migrations[path] = (value, source)
    return value


def service_keyfile(service: Service, state: str) -> Path:
    """Use the operator's keys reference, just as the service does."""
    config = Path(state) / "services" / service.short / "config.json"
    value = service_settings(service, state).get("keys")
    if not isinstance(value, dict) or set(value) != {"file"}:
        raise ValueError(f"{config}: setup needs keys with exactly file")
    from boring_service_kit.http import ServiceConfig
    return ServiceConfig.file(config, value["file"], "keys file")


def check_config_ports(state: str, selected: list[str]) -> dict[Path, tuple[dict[str, Any], disk.Version]]:
    """Read selected and already configured listeners before any installation effects."""
    ports: dict[int, str] = {}
    migrations: dict[Path, tuple[dict[str, Any], disk.Version]] = {}
    for short, service in SERVICES.items():
        config = Path(state) / "services" / short / "config.json"
        if short not in selected and not config.exists():
            continue
        value = service_settings(service, state, migrations=migrations)
        listeners = [(value.get("port", urlsplit(value["url"]).port), str(config))]
        for port, filename in listeners:
            if port in ports:
                raise ValueError(f"{filename}: port {port} is also used by {ports[port]}")
            ports[port] = filename
    return migrations


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
    return ["--config", config, "--state-dir", os.path.join(folder, "state"), "--unsafe-url-auth"]


def gateway_folder(state: str) -> Path:
    return Path(state) / "services" / GATEWAY


def headers_file(state: str, short: str) -> Path:
    return Path(state) / "services" / short / "client.headers.json"


def read_bearer(path: Path) -> str:
    value = read_settings(path)
    token = held_key({"headers": value})
    if set(value) != {"Authorization"} or token is None:
        raise ValueError(f"{path}: expected a bearer Authorization header; inspect private credentials")
    return token


def credential(service: Service, state: str, entry: Any, subject: str,
               now: float, rotate: bool, apply: bool) -> tuple[str | None, bool]:
    """Persist the bearer before its hash; rerun repairs the same pair.

    The header file owns the bearer after creation. Legacy imports must match
    an accepted setup key. Other subjects/labels in keys.json are retained.
    Callers hold the key-file lock across read, header publication and save.
    """
    path = headers_file(state, service.short)
    keyfile = service_keyfile(service, state)
    try:
        current = keys.KeyFile.load(keyfile) if keyfile.exists() else keys.KeyFile(
            f"key:{subject.rsplit('@', 1)[1]}-{service.short}", ())
    except (OSError, ValueError) as error:
        raise ValueError(f"{keyfile}: cannot read accepted keys ({type(error).__name__})") from None
    owned_keys = [k for k in current.keys if k.subject == subject and k.label == LABEL]
    if path.exists():
        token = read_bearer(path)
    else:
        candidates = []
        direct = held_key(entry)
        legacy = gateway_folder(state) / f"{service.short}.headers.json"
        if direct:
            candidates.append(direct)
        if legacy.exists():
            candidates.append(read_bearer(legacy))
        if len(set(candidates)) > 1:
            raise ValueError(f"{path}: conflicting legacy credentials; inspect before setup")
        token = candidates[0] if candidates else None
        if token and not any(k.sha256 == keys.fingerprint(token) for k in owned_keys):
            raise ValueError(f"{keyfile}: legacy bearer does not match an accepted setup key")
        if token is None and owned_keys and not rotate:
            raise ValueError(f"{path}: missing bearer for existing setup keys; restore it or use -RotateKeys")
    found = next((k for k in current.keys if token and k.sha256 == keys.fingerprint(token)), None)
    if found and (found.subject != subject or found.label != LABEL or found.role != "writer"):
        raise ValueError(f"{keyfile}: bearer belongs to a different grant; inspect private credentials")
    if found and found.expires <= now and not rotate:
        raise ValueError(f"{keyfile}: setup key expired; use -RotateKeys")
    new_key = rotate or token is None
    if not apply:
        return token, new_key
    if new_key:
        token = keys.new_key()
        found = None
    if not path.exists() or rotate:
        write_private(path, {"Authorization": f"Bearer {token}"})
    if found is None:
        created = int(now)
        accepted = keys.Key("k" + keys.fingerprint(token)[:16], keys.fingerprint(token),
                            subject, "writer", created + LOCAL_DAYS * keys.DAY, created, LABEL)
    elif found.expires - found.created < LOCAL_DAYS * keys.DAY:
        # Upgrade valid annual metadata once, retaining the bearer and original id.
        accepted = keys.Key(found.id, found.sha256, found.subject, found.role,
                            int(now) + LOCAL_DAYS * keys.DAY, found.created, found.label)
    else:
        accepted = found
    retained = tuple(k for k in current.keys if k not in owned_keys)
    wanted = keys.KeyFile(current.issuer, retained + (accepted,))
    if wanted != current:
        wanted.save(keyfile)
    return token, new_key


def write_private(path: Path, data: Any, *, expected: disk.Version | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    if expected is None and path.exists():
        previous, expected = disk.read_version(path, 512 * 1024)
        if previous == raw:
            return
    disk.replace_atomically(path, raw, create=expected is None, expected=expected)


GATEWAY_CALL_SECONDS = 900


def ferry_config(root: str, state: str, carried: list[str]) -> dict[str, Any]:
    """Each gated service uses its local guide when present, with a release fallback."""
    backends: dict[str, Any] = {}
    for short in carried:
        # A read timeout for every backend call: without one a wedged backend holds the gateway's request
        # for good. Above the longest tool (d365_run, 600 s), so it never cuts a working call.
        backend: dict[str, Any] = {"upstream": {"url": service_settings(SERVICES[short], state)["url"], "headers_file": f"../{short}/client.headers.json",
                                                "timeout_seconds": GATEWAY_CALL_SECONDS}}
        if short in GUIDES:
            page = GUIDES[short]
            guide = Path(kit_folder(state)) / page
            if not guide.is_file():
                guide = Path(root) / KIT / page
            if guide.is_file():
                backend["guide_file"] = str(guide)
        backends[short] = backend
    return {"backends": backends, "gateways": {GATEWAY_NAME: {"service": "config.json", "backends": carried}}}


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
    report["differs"] = sorted(n for n in mine if mine[n] != theirs.get(n))
    report["new_pages"] = sorted(theirs.keys() - mine.keys())
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
    # A rename fails when the folder appeared meanwhile: then it is not ours to replace,
    # and the staging copy must not stay behind next to it.
    try:
        os.rename(staging, local)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if not local.exists():
            raise


def owned(service: Service, entry: Any, state: str | None = None) -> bool:
    """An owned entry follows its config; a matching setup bearer also proves an old port."""
    if not isinstance(entry, dict) or entry.get("type") != "http":
        return False
    url = service_settings(service, state)["url"] if state is not None else service.url
    if entry.get("url") in (service.url, url):
        return True
    if state is None or not isinstance(entry.get("url"), str):
        return False
    try:
        parsed = urlsplit(entry["url"])
    except ValueError:
        return False
    token = held_key(entry)
    keyfile = service_keyfile(service, state)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/mcp" or token is None or not keyfile.exists():
        return False
    return any(k.sha256 == keys.fingerprint(token) and k.label == LABEL for k in keys.KeyFile.load(keyfile).keys)


def held_key(entry: Any) -> str | None:
    headers = entry.get("headers") if isinstance(entry, dict) else None
    value = headers.get("Authorization") if isinstance(headers, dict) else None
    if isinstance(value, str) and value.startswith("Bearer " + keys.PREFIX):
        return value[len("Bearer "):]
    return None


def describe(service: Service, root: str, state: str, index: str, subject: str) -> dict[str, Any]:
    folder = os.path.join(state, "services", service.short)
    windowless = os.path.join(root, "python", "pythonw.exe")  # the logon tasks'
    config = service_settings(service, state)
    return {"name": service.name, "url": config["url"], "folder": folder, "subject": subject,
            "task": f"pondNT {service.name}", "module": service.module,
            "python": windowless,
            "account": "user", "highest": service.short in ("core", "d365", "d365whsapp", "d365web", "d365data"),
            "arguments": arguments(service, folder, index, state),
            "log": os.path.join(folder, "service.log")}


def plan(servers: dict[str, Any], root: str, state: str, index: str, only: list[str],
         user: str, computer: str, apply: bool = False, now: float | None = None,
         rotate_keys: bool = False, installed: tuple[str, ...] = ()) -> dict[str, Any]:
    now = time.time() if now is None else now
    subject = f"{user}@{computer}".lower()
    selected = list(dict.fromkeys([*only, *(s for s in installed if s != GATEWAY)]))
    migrations = check_config_ports(state, selected)
    result: dict[str, Any] = {"services": [], "conflicts": [], "removals": []}
    # Validate the whole scope and gateway dependencies before any write.
    for short in selected:
        service = SERVICES[short]
        current = servers.get(service.name)
        if current is not None and not owned(service, current, state):
            result["conflicts"].append(f"{service.name}: customized entry retained; not configured")
        elif short == "sourcecache" and not index:
            result["conflicts"].append("boring-sourcecache: no source index; build it first")
        elif short in OPT_IN_CONFIGS and not (Path(state) / "services" / short / OPT_IN_CONFIGS[short][0]).is_file():
            name, doc = OPT_IN_CONFIGS[short]
            result["conflicts"].append(f"{service.name}: no {name} in {Path(state) / 'services' / short}; see {doc}")
        else:
            credential(service, state, current, subject, now, rotate_keys, False)
            result["services"].append(describe(service, root, state, index, subject))
    available = {s["name"] for s in result["services"]}
    for short in installed:
        if short != GATEWAY and SERVICES[short].name not in available:
            raise ValueError(f"installed {short} could not be prepared; resolve its conflict before updating")
    gated = [short for short in selected if short not in NEVER_GATED
             and SERVICES[short].name in available]
    if gated:
        gateway = SERVICES[GATEWAY]
        current = servers.get(gateway.name)
        if current is not None and not owned(gateway, current, state):
            result["conflicts"].append(f"{gateway.name}: customized entry retained; not configured")
            return {**result, "services": []}
        credential(gateway, state, current, subject, now, rotate_keys, False)
        result["services"].append(describe(gateway, root, state, index, subject))
    # Only configurations for tasks that will be prepared are upgraded.
    for item in result["services"]:
        config = Path(item["folder"]) / "config.json"
        if config in migrations:
            item["config_change"] = "config.json: adds mode=keys"
            if apply:
                value, source = migrations[config]
                write_private(config, value, expected=source)
    if "core" in only:
        report = kit_report(root, state)
        result["handbook"] = report
        if report["state"] == "no-bootstrap":
            result["conflicts"].append(f"handbook: {report['folder']} has no bootstrap.md; left as it is")
        if apply and report["state"] == "new":
            seed_kit(root, state)
            result["handbook"] = {**kit_report(root, state), "seeded": True}
    for item in result["services"]:
        short = next(n for n, svc in SERVICES.items() if svc.name == item["name"])
        service = SERVICES[short]
        current = servers.get(service.name)
        folder = Path(item["folder"])
        if apply:
            folder.mkdir(parents=True, exist_ok=True)
            config = folder / "config.json"
            if not config.exists():
                write_private(config, service_settings(service, state))
            keyfile = service_keyfile(service, state)
            keyfile.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(keyfile.with_name("." + keyfile.name + ".lock")):
                token, new = credential(service, state, current, subject, now, rotate_keys, True)
        else:
            token, new = credential(service, state, current, subject, now, rotate_keys, False)
        behind = short in gated
        entry = {**(current or {}), "type": "http", "url": item["url"],
                 "headers": {"Authorization": f"Bearer {token}"}}
        item.update(new_key=new, behind=SERVICES[GATEWAY].name if behind else None,
                    entry_changes=current is not None if behind else current != entry)
        if behind:
            if current is not None:
                result["removals"].append(service.name)
        elif apply:
            item["entry"] = entry
    if gated:
        result["gateway"] = {"name": SERVICES[GATEWAY].name, "carries": [SERVICES[s].name for s in gated]}
        if apply:
            write_private(gateway_folder(state) / FERRY, ferry_config(root, state, gated))
    return result


def uninstall(servers: dict[str, Any], state: str, root: str = "", index: str = "",
              installed: tuple[str, ...] = ()) -> dict[str, Any]:
    """Remove verified tasks/client entries; keep configuration, keys and data."""
    ours = [svc for short, svc in SERVICES.items()
            if owned(svc, servers.get(svc.name), state) or short in installed]
    subject = f"{getpass.getuser()}@{platform.node()}".lower()
    specs = [describe(svc, root, state, index, subject) for svc in ours
             if not (svc.short == "sourcecache" and not index)
             and not (svc.short in OPT_IN_CONFIGS and not
                      (Path(state) / "services" / svc.short / OPT_IN_CONFIGS[svc.short][0]).is_file())]
    return {"removals": [svc.name for svc in ours if owned(svc, servers.get(svc.name), state)],
            "conflicts": [f"{svc.name}: customized entry retained" for svc in SERVICES.values()
                          if svc.name in servers and not owned(svc, servers[svc.name], state)],
            "tasks": [f"pondNT {svc.name}" for svc in ours],
            "folders": [os.path.join(state, "services", svc.short) for svc in ours],
            "handbook": kit_folder(state), "services": specs}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True)
    # %% : argparse formats help strings (Python 3.14 checks them when the argument is added).
    parser.add_argument("--state", required=True, help="e.g. %%LOCALAPPDATA%%\\boring-pondnt")
    parser.add_argument("--index", default="")
    parser.add_argument("--only", default=DEFAULT_ONLY)
    parser.add_argument("--installed", default="", help="current owned task names, observed by setup")
    parser.add_argument("--describe", action="store_true", help="read-only task templates and fresh defaults")
    parser.add_argument("--plan", action="store_true", help="look only; write nothing")
    parser.add_argument("--rotate-keys", action="store_true", help="explicitly replace selected setup bearers")
    parser.add_argument("--uninstall", action="store_true", help="what -Uninstall removes; writes nothing")
    args = parser.parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in only if s not in SERVICES or s == GATEWAY]
    if unknown:
        parser.error(f"unknown services: {', '.join(unknown)}")
    if args.describe:
        subject = f"{getpass.getuser()}@{platform.node()}".lower()
        configured = [short for short in SERVICES if short != GATEWAY
                      and (Path(args.state) / "services" / short).is_dir()]
        print(json.dumps({"default_services": DEFAULT_ONLY.split(","), "configured_services": configured,
                          "services": [{"short": short, **describe(svc, args.root, args.state, args.index, subject)}
                                       for short, svc in SERVICES.items()]}))
        return
    installed = tuple(s for s in args.installed.split(",") if s)
    if any(s not in SERVICES for s in installed):
        parser.error("installed task names must be known services")
    servers = json.load(sys.stdin)
    if not isinstance(servers, dict):
        parser.error("stdin must be the servers object of .mcp.json")
    if args.uninstall and (args.plan or args.rotate_keys):
        parser.error("uninstall cannot be combined with plan or rotate-keys")
    # Refusals (a corrupt ferry.json, a missing key file, a customized gateway) are ValueErrors:
    # one line on stderr and exit 2, not a traceback; setup.ps1 stops on the exit code.
    try:
        if args.uninstall:
            answer = uninstall(servers, args.state, args.root, args.index, installed)
        else:
            answer = plan(servers, args.root, args.state, args.index, only, getpass.getuser(), platform.node(),
                          apply=not args.plan, rotate_keys=args.rotate_keys, installed=installed)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
