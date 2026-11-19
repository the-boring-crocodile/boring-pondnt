"""The optional desktop Pond tunnel; normal setup never invokes this module."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

MODULES = {"core": "boring_pondnt.server", "d365": "boring_pondnt.d365_server",
           "d365whsapp": "boring_pondnt.d365whsapp_server", "d365web": "boring_pondnt.d365web_server",
           "d365data": "boring_pondnt.d365data_server"}
PORTS = dict(zip(MODULES, (18530, 18532, 18534, 18536, 18538), strict=True))
# Retain the only job handle until process teardown: closing it kills this process too.
_WINDOW_JOB = None


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: cannot read JSON ({type(error).__name__})") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def file_at(folder: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{folder}: expected a nonempty file path")
    path = Path(os.path.expandvars(value))
    path = path if path.is_absolute() else folder / path
    if not path.is_file():
        raise ValueError(f"{path}: required file is missing")
    return path.resolve()


def port_number(value, where: str) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError(f"{where}: port must be an integer from 1024 to 65535")
    return value


def check_ports(ports: dict[int, str]) -> None:
    for port, name in ports.items():
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError(f"{name}: Windows loopback port {port} is already in use; choose a free port") from None


def plan(settings: Path, python: str = sys.executable) -> dict:
    """Validate every selected input before creating state or starting any process."""
    data = read_json(settings)
    replacements = {"access": "boring-crocodile-admin service-grants NAME --callers NAME=ROLE",
                    "control_plane": "crocodile_url from boring-crocodile-admin list --json",
                    "control_port": "crocodile_url; the private control-plane forward is removed"}
    retired = sorted(set(data) & replacements.keys())
    if retired:
        raise ValueError("; ".join(key + " is replaced by " + replacements[key] for key in retired))
    unknown = set(data) - {"box", "ssh_host", "identity_file", "known_hosts_file",
                           "crocodile_url", "servers"}
    if unknown:
        raise ValueError(f"{settings}: unknown fields {sorted(unknown)}")
    box = data.get("box", "devbox")
    if not isinstance(box, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", box):
        raise ValueError(f"{settings}: box must be a lower-case name, at most 24 characters")
    host = data.get("ssh_host", "")
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
        raise ValueError(f"{settings}: ssh_host must be user@host")
    folder = settings.resolve().parent
    identity = file_at(folder, data.get("identity_file"))
    known_hosts = file_at(folder, data.get("known_hosts_file"))
    ports: dict[int, str] = {}
    from boring_service_kit.http import ServiceConfig
    servers = data.get("servers")
    if not isinstance(servers, dict) or not servers or set(servers) - MODULES.keys():
        raise ValueError(f"{settings}: servers must select core and/or the four D365 servers")
    prepared = []
    for short, item in servers.items():
        if not isinstance(item, dict) or set(item) - {"port", "client_id_file", "secret_file", "settings_file", "web"}:
            raise ValueError(f"{settings}: invalid settings for {short}")
        name = box if short == "core" else f"{box}-{short}"
        port = port_number(item.get("port", PORTS[short]), name)
        if port in ports:
            raise ValueError(f"{name}: port {port} is also selected by {ports[port]}")
        ports[port] = name
        client_file = file_at(folder, item.get("client_id_file"))
        client_id = client_file.read_text(encoding="utf-8-sig").strip()
        secret = file_at(folder, item.get("secret_file"))
        if not client_id or not secret.stat().st_size:
            raise ValueError(f"{name}: empty client identity or secret file")
        state = folder / "servers" / name
        config = {"url": f"http://127.0.0.1:{port}/mcp", "mode": "crocodile",
                  "crocodile": {"url": data.get("crocodile_url"), "client_id": client_id,
                                "secret_file": str(secret)}}
        ServiceConfig.inspect_value(config, state / "config.json")
        argv = [python, "-X", "utf8", "-m", MODULES[short]]
        if short != "core":
            argv += ["serve"]
        if short == "d365web":
            from .d365web_client import Config
            web = item.get("web")
            if not isinstance(web, dict):
                raise ValueError(f"{settings}: d365web needs its own web settings")
            web = {**web, "profiles_dir": str(state / "edge")}
            try:
                Config(**web).validate()
            except (TypeError, ValueError):
                raise ValueError(f"{settings}: invalid d365web web settings") from None
            debug = port_number(web.get("debug_port"), f"{name} Edge")
            if debug in ports:
                raise ValueError(f"{name} Edge: port {debug} is also selected by {ports[debug]}")
            ports[debug] = f"{name} Edge"
            argv += ["--d365web", str(state / "d365web.runtime.json")]
        else:
            web = None
            if short in ("d365whsapp", "d365data"):
                extra = file_at(folder, item.get("settings_file"))
                read_json(extra)
                argv += [f"--{short}", str(extra)]
        argv += ["--config", str(state / "config.json"), "--state-dir", str(state)]
        prepared.append({"name": name, "port": port, "state": state, "config": config, "argv": argv, "web": web})
    check_ports(ports)
    ssh = ["ssh.exe", "-F", "none", "-i", str(identity), "-o", "IdentitiesOnly=yes",
           "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
           "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15", "-N"]
    for server in prepared:
        port = server["port"]
        ssh += ["-R", f"127.0.0.1:{port}:127.0.0.1:{port}"]
    ssh.append(host)
    return {"servers": prepared, "ssh": ssh, "box": box}


def contain_window() -> None:
    """Children inherit our job; only this process holds its kill-on-close handle."""
    global _WINDOW_JOB
    import win32api
    import win32job
    job = win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32api.SetHandleInformation(job, 1, 0)  # HANDLE_FLAG_INHERIT must be off
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except BaseException:
        win32api.CloseHandle(job)
        raise
    _WINDOW_JOB = job


def stop(children: list) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def run(prepared: dict, *, popen=subprocess.Popen, sleep=time.sleep) -> None:
    children = []
    ssh = None
    try:
        for server in prepared["servers"]:
            state = server["state"]
            state.mkdir(parents=True, exist_ok=True)
            (state / "config.json").write_text(json.dumps(server["config"], indent=2) + "\n", encoding="utf-8")
            if server["web"] is not None:
                (state / "d365web.runtime.json").write_text(json.dumps(server["web"], indent=2) + "\n", encoding="utf-8")
            children.append(popen(server["argv"], cwd=state, close_fds=True))
            print(f"{server['name']}: starting on {server['port']}", flush=True)
        while True:
            for server, child in zip(prepared["servers"], children, strict=True):
                if child.poll() is not None:
                    raise RuntimeError(f"{server['name']}: server exited ({child.returncode}); close and reopen after fixing it")
            if ssh is None:
                ssh = popen(prepared["ssh"], close_fds=True)
                print("SSH connecting; close this window to stop the Pond tunnel and its servers.", flush=True)
            elif ssh.poll() is not None:
                print(f"SSH exited ({ssh.returncode}); retrying in 5 seconds. Servers stay running.", flush=True)
                sleep(5)
                ssh = None
            sleep(0.5)
    finally:
        stop([*children, *([ssh] if ssh is not None else [])])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError("Run pond-tunnel.cmd as administrator on the desktop.")
        prepared = plan(args.settings)
        contain_window()  # no child may start before this succeeds
        run(prepared)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
