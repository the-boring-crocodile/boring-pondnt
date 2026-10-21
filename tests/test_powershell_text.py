"""Static checks on the shipped PowerShell scripts (no PowerShell needed)."""
import re
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def test_no_variable_swallows_a_question_mark():
    # In PowerShell "?" is a legal variable-name character: "$McpJson?" reads the empty
    # variable "McpJson?". Found on a dev VM. Write "${McpJson}?".
    bad = []
    for script in TOOLS.glob("*.ps1"):
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<!\$\{)\$[A-Za-z_][A-Za-z0-9_]*\?", line):
                bad.append(f"{script.name}:{number}: {line.strip()}")
    assert not bad, bad
