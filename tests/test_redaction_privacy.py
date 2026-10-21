"""Synthetic forms reported by the audit, including nested structured values."""
import json
import pytest
from boring_pondnt.d365_server import redact


@pytest.mark.parametrize("value", [
    '{"Password":"SYNTHETIC_A","safe":"keep"}',
    "-DatabasePassword SYNTHETIC_B",
    "SqlPassword=SYNTHETIC_C",
    "Password=FIRST;SYNTHETIC_D",
    {"Password": "SYNTHETIC_E", "safe": [{"ClientSecret": "SYNTHETIC_F"}]},
])
def test_reported_secret_forms_never_leave_the_server(value):
    assert "SYNTHETIC_" not in json.dumps(redact(value))


def test_structured_redaction_preserves_nonsensitive_diagnostics():
    value = {"rows": [{"message": "build done", "DatabasePassword": ["hidden"]}], "count": 1}
    result = redact(value)
    assert result["rows"][0]["message"] == "build done"
    assert result["rows"][0]["DatabasePassword"] == "***"
    assert result["count"] == 1
