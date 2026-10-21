"""Probe: what bytes does cmd write, and in which encoding? (run with the release's Python)

Records the raw output of a few ways to start cmd, so the fix for cmd's
encoding follows from data, not from guesses about code pages.

    python probe_cmd.py <result.json>
"""
import ctypes
import json
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
MARKER = "Gr\u00fc\u00dfe \u00e4\u00f6\u00fc"
kernel32 = ctypes.windll.kernel32


def run(cmdline, no_window):
    flags = CREATE_NEW_PROCESS_GROUP | (CREATE_NO_WINDOW if no_window else 0)
    proc = subprocess.run(cmdline, capture_output=True, creationflags=flags, timeout=30)
    out = proc.stdout
    decoded = {}
    for enc in ("utf-8", "utf-16-le", f"cp{kernel32.GetACP()}", f"cp{kernel32.GetOEMCP()}"):
        try:
            decoded[enc] = out.decode(enc).strip()
        except (UnicodeDecodeError, LookupError) as exc:
            decoded[enc] = f"<{type(exc).__name__}>"
    return {"cmdline": cmdline, "no_window": no_window, "exit": proc.returncode,
            "stdout_hex": out.hex(" "), "stderr": proc.stderr.decode("latin-1")[:200],
            "decodes_as_marker": [e for e, d in decoded.items() if d.endswith(MARKER)], "decoded": decoded}


result = {
    "python": sys.version,
    "acp": kernel32.GetACP(),
    "oemcp": kernel32.GetOEMCP(),
    "probe_console_cp": kernel32.GetConsoleCP(),
    "probe_console_output_cp": kernel32.GetConsoleOutputCP(),
    "variants": [],
}
for no_window in (True, False):
    for cmdline in (
        f'cmd.exe /d /s /c "chcp 65001 >nul & echo {MARKER}"',  # what execs.py does today
        f'cmd.exe /d /s /c "echo {MARKER}"',
        f'cmd.exe /d /u /s /c "echo {MARKER}"',
        'cmd.exe /d /s /c "chcp"',
    ):
        try:
            result["variants"].append(run(cmdline, no_window))
        except Exception as exc:  # a probe reports, it does not stop
            result["variants"].append({"cmdline": cmdline, "no_window": no_window, "error": repr(exc)})
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False, indent=1)
print("probe written")
