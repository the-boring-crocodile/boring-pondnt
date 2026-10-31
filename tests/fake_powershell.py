#!/usr/bin/env python3
"""A stand-in for Windows PowerShell with d365fo.tools, for boring-pondnt-d365's tests.

Called like powershell.exe (`... -EncodedCommand <base64 UTF-16>`). The session
loop (it contains `@@READY`) gets the line protocol with canned answers; any
other command is a job: it prints a few lines -- one with a password, as
d365fo.tools does -- and exits. FAKE_PS_MODE=no_module makes the import fail.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SECRET_LINE = ("SyncEngine.exe -connect=\"server=BOX;Database=AxDB; User Id=axdbadmin;"
               "Password='p@ss=w;rd';\" -verbosity=normal")
ANSWERS = [
    ("Get-D365Environment -All | ForEach-Object {\n    [ordered]@{ name", {
        "services": [{"name": "AOSService", "status": "Running", "start": "Automatic"},
                     {"name": "DynamicsAxBatch", "status": "Stopped", "start": "Automatic"}],
        "installed": [{"service": "AOSService", "version": "7.0.7452.66"}],
        "url": "https://box.example", "elevated": True, "packages": "J:\\AosService\\PackagesLocalDirectory",
        "aos": "Stopped"}),
    ("Get-D365Model", [{"name": "Foundation", "module": "ApplicationSuite", "layer": 0,
                        "publisher": "Microsoft Corporation", "version": "10.33.1620.58155",
                        "customizable": "Allow", "binary": False},
                       {"name": "ContosoExt", "module": "ContosoExt", "layer": 10, "publisher": "Contoso",
                        "version": "1.0.0.0", "customizable": "Allow", "binary": False}]),
    ("Start-D365Environment -All", None),
    ("D365Environment -Aos", None),
    ("Get-D365Environment -All | ForEach-Object { [ordered]@{ name = $_.Name; status", [
        {"name": "AOSService", "status": "Running"}, {"name": "DynamicsAxBatch", "status": "Running"}]),
]


def command() -> str:
    args = sys.argv[1:]
    return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")


def child() -> None:
    path = os.environ.get("FAKE_PS_CHILD_PID_FILE")
    if path:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        Path(path).write_text(str(process.pid))


def session() -> None:
    if os.environ.get("FAKE_PS_MODE") == "no_module":
        ready = {"ok": False, "error": "The specified module 'd365fo.tools' was not loaded"}
    else:
        ready = {"ok": True, "version": "0.7.34-fake"}
    if os.environ.get("FAKE_PS_MODE") == "bad_ready":
        ready = []
    print("@@READY " + json.dumps(ready), flush=True)
    for line in sys.stdin:
        script = base64.b64decode(line.strip()).decode("utf-8")
        if "Oversized-Line" in script:
            sys.stdout.write("x" * (2 * 1024 * 1024))
            sys.stdout.flush()
            time.sleep(3600)
        if "Malformed-Reply" in script:
            print("@@RESULT []", flush=True)
            continue
        if "Sleep-Forever" in script:
            child()
            marker = os.environ.get("FAKE_PS_CALL_MARKER")
            if marker:
                Path(marker).write_text("entered")
            time.sleep(3600)
        print("WARNING: some host noise " + SECRET_LINE, flush=True)
        if "Throw-Me" in script or "Stop-D365Environment -Batch" in script:
            print("@@RESULT " + json.dumps({"ok": False, "error": "it failed: " + SECRET_LINE}), flush=True)
            continue
        if "found = $false" in script and "NoLog" in script:
            print("@@RESULT " + json.dumps({"ok": True, "value": {"found": False, "folder": "C:\\x"}}), flush=True)
            continue
        if "-OutputCommandOnly" in script:  # d365fo.tools writes the command to the host, returns nothing
            print(SECRET_LINE, flush=True)
            print("@@RESULT " + json.dumps({"ok": True, "value": None}), flush=True)
            continue
        if "| Out-String -Width 220" in script:  # d365_run: the console text, with a labelled password
            text = "Name       Status\nAOSService Running\nconnect: " + SECRET_LINE + "\n"
            if "Long-Output" in script:
                text = "y" * 95 + " " + SECRET_LINE + "\n" + "z" * 300
            print("@@RESULT " + json.dumps({"ok": True, "value": text}), flush=True)
            continue
        value = next((answer for needle, answer in ANSWERS if needle in script), None)
        print("@@RESULT " + json.dumps({"ok": True, "value": value}), flush=True)


def job(text: str) -> None:
    child()
    print("Import done", flush=True)
    print(SECRET_LINE, flush=True)
    if "SlowJob" in text:
        time.sleep(3600)
    print("Build completed: 0 errors", flush=True)


if __name__ == "__main__":
    text = command()
    if "@@READY" in text:
        session()
    else:
        job(text)
