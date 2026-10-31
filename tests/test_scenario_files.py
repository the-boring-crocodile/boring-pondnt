"""Scenario files assume nothing about the box: every environment value is a named parameter."""

import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# The neutral example ships with the tests; a private checkout may keep its own scenario files in
# pond/scenarios (never published), and they are checked the same way when present.
SCENARIOS = (sorted(HERE.joinpath("scenarios").rglob("*.json"))
             + sorted(HERE.parent.joinpath("pond", "scenarios").rglob("*.json")))
VARIABLE = re.compile(r"\$\{(\w+)\}")


def saved_names(value):
    """Names a step saves (web_run_scenario `save`) anywhere below value."""
    if isinstance(value, dict):
        found = set(value["save"]) if isinstance(value.get("save"), dict) else set()
        for item in value.values():
            found |= saved_names(item)
        return found
    if isinstance(value, list):
        return set().union(*map(saved_names, value)) if value else set()
    return set()


def test_there_are_scenario_files():
    assert SCENARIOS


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_scenario_file_names_its_assumptions(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    parameters = data["parameters"]
    assert parameters and all(set(p) == {"example", "about"} for p in parameters.values())
    known = set(parameters) | {"run"}
    for index, part in enumerate(data["parts"], 1):
        assert VARIABLE.fullmatch(part["service"]), f"part {index}: the service must be a parameter"
        unknown = set(VARIABLE.findall(json.dumps(part))) - known
        assert not unknown, f"part {index} uses {sorted(unknown)} before anything defines them"
        known |= saved_names(part)
    rest = json.dumps({k: v for k, v in data.items() if k != "parameters"})
    unused = set(parameters) - set(VARIABLE.findall(rest))
    assert not unused, f"unused parameters {sorted(unused)}"
    limits = {"whs_run_scenario": 50, "web_run_scenario": 80}  # the servers' caps
    for index, part in enumerate(data["parts"], 1):
        if part["tool"] in limits:
            assert len(part["arguments"]["steps"]) <= limits[part["tool"]], f"part {index}: too many steps for one call"
    for part in data["parts"]:  # an open without a company would depend on the signed-in user's default
        for step in part["arguments"].get("steps", []):
            if step.get("op") == "open":
                assert "company" in step, f"open {step['menu_item']} without a company"
