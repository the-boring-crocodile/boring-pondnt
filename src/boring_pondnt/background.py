"""Run the core without a console window, its output in a log file.

The core's scheduled task runs as the person at logon, in their session: under
python.exe that would open a console window on their desktop. pythonw.exe has
none, and no stdout/stderr either, so this sends both to the log first:

    pythonw.exe -X utf8 -m boring_pondnt.background --log <file> boring_pondnt.server --config <file>
    pythonw.exe -X utf8 -m boring_pondnt.background --log <file> boring_pondnt.d365_server serve --config <file>
    (likewise boring_pondnt.d365whsapp_server and boring_pondnt.d365web_server, opt-in)

Child processes (exec, jobs) are started with CREATE_NO_WINDOW already
(boring_service_kit.process), so they open no windows either.
"""
from __future__ import annotations

import runpy
import sys

# What a task may start this way: every service setup runs in the person's account
# (setup_services.SERVICES with account="user"; a test keeps the two in step).
ALLOWED = {"boring_pondnt.server", "boring_pondnt.d365_server",
           "boring_pondnt.d365whsapp_server", "boring_pondnt.d365web_server"}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--log" or args[2] not in ALLOWED:
        raise SystemExit(f"usage: --log <file> {{{','.join(sorted(ALLOWED))}}} [arguments]")
    log = open(args[1], "a", encoding="utf-8", buffering=1)  # noqa: SIM115 -- lives as long as the process
    sys.stdout = sys.stderr = log
    sys.argv = [args[2], *args[3:]]
    runpy.run_module(args[2], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
