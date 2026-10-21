"""Probe: Defender on this dev box, read-only (run with the release's Python).

Runs tools/devbox-defender.ps1 in its default report mode -- it changes
nothing -- and records what it says: Defender's state, existing exclusions,
what it would add. Paths only, no D365 content.

    python probe_defender.py <result.json>
"""
import json
import subprocess
import sys
from pathlib import Path

script = Path(__file__).with_name("devbox-defender.ps1")
result = {"script": str(script), "exists": script.is_file()}
if script.is_file():
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                          capture_output=True, timeout=300)
    result["exit"] = proc.returncode
    result["report"] = proc.stdout.decode("utf-8", errors="replace").splitlines()
    result["stderr"] = proc.stderr.decode("utf-8", errors="replace")[-2000:]
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1)
print("probe written")
