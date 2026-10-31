"""What web and WHS scenarios share: `${name}` variables and objects of scalar values.

The two step formats differ (web steps save page data with `save` and regular expressions, WHS steps
do not), so d365web and whs_steps keep their own step validators. Variable substitution and the
"object with nonempty names and scalar values" rule are one responsibility, here. Each caller passes
the error it refuses with (`refuse(message)`), so no server depends on another's error type.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
Refuse = Callable[[str], Exception]


def substitute(value: Any, variables: dict[str, str], refuse: Refuse) -> Any:
    """${name} in strings (recursively in lists and objects); an unknown name is refused."""
    if isinstance(value, str):
        def one(match: re.Match) -> str:
            name = match.group(1)
            if name not in variables:
                raise refuse(f"unknown variable ${{{name}}}")
            return str(variables[name])
        return VARIABLE.sub(one, value)
    if isinstance(value, list):
        return [substitute(v, variables, refuse) for v in value]
    if isinstance(value, dict):
        return {substitute(k, variables, refuse): substitute(v, variables, refuse) for k, v in value.items()}
    return value


def check_scalar_map(value: Any, name: str, refuse: Refuse) -> None:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k.strip() for k in value):
        raise refuse(f"{name}: expected an object with nonempty names")
    if any(v is not None and type(v) not in (str, int, float, bool) for v in value.values()):
        raise refuse(f"{name}: values must be scalar")
