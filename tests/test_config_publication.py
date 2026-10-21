"""Installer CLI must preserve an intervening edit and never echo its JSON."""
import json
import os
import subprocess
import sys
from boring_pondnt import disk


def test_config_publication_roundtrip_and_stale_refusal(tmp_path):
    path = tmp_path / "config.json"
    value = {"servers": {"fixture": {"secret": "SYNTHETIC_PRIVATE", "path": "Grüße"}}}
    # Embeddable Windows Python ignores PYTHONPATH when ._pth is present.
    # Pass the tested import paths explicitly, and verify the child source.
    bootstrap = (
        "import sys, runpy; "
        f"sys.path[:] = {sys.path!r}; "
        "from pathlib import Path; from boring_pondnt import disk; "
        f"assert Path(disk.__file__).resolve() == Path({disk.__file__!r}).resolve(); "
        "runpy.run_module('boring_pondnt.publish_config', run_name='__main__')"
    )
    command = [sys.executable, "-I", "-c", bootstrap, "--path", str(path)]
    created = subprocess.run(command, input=json.dumps(value), text=True, capture_output=True)
    assert created.returncode == 0 and created.stdout == ""
    sha = disk.sha256_bytes(path.read_bytes())
    path.write_text('{"intervening":"keep"}')
    refused = subprocess.run(command + ["--expected-sha", sha],
                             input=json.dumps(value), text=True, capture_output=True)
    assert refused.returncode != 0
    assert "SYNTHETIC_PRIVATE" not in refused.stdout + refused.stderr
    assert json.loads(path.read_text()) == {"intervening": "keep"}


def test_failed_config_publication_keeps_readable_original(tmp_path, monkeypatch, capsys):
    import io
    from boring_pondnt import publish_config, windows_files
    path = tmp_path / "config.json"
    original = b'{"servers":{"fixture":{"secret":"SYNTHETIC_OLD"}}}'
    path.write_bytes(original)
    def fail(*args):
        raise OSError("injected before replacement")
    if os.name == "nt":
        monkeypatch.setattr(windows_files, "_replace_native", fail)
    else:
        monkeypatch.setattr(disk.os, "replace", fail)
    monkeypatch.setattr(sys, "argv", ["publish_config", "--path", str(path),
                                      "--expected-sha", disk.sha256_bytes(original)])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"servers":{"new":"SYNTHETIC_NEW"}}'))
    assert publish_config.main() == 1
    assert path.read_bytes() == original
    assert json.loads(path.read_bytes())["servers"]["fixture"]["secret"] == "SYNTHETIC_OLD"
    assert list(tmp_path.iterdir()) == [path]
    output = capsys.readouterr()
    assert "SYNTHETIC" not in output.out + output.err
