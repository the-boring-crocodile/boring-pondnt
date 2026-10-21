"""The write probe owns only the file it creates."""
from boring_pondnt import selfcheck


def test_selfcheck_preserves_preexisting_probe_name(tmp_path, monkeypatch):
    sentinel = tmp_path / ".self-check"
    sentinel.write_bytes(b"operator-owned")
    monkeypatch.setattr(selfcheck, "IS_WINDOWS", False)
    for name in ("_shell_check", "_timeout_check", "_text_check", "_job_check", "_long_paths"):
        monkeypatch.setattr(selfcheck, name, lambda *args: "not part of this test")
    result = selfcheck.run_checks(tmp_path)
    assert next(c for c in result["checks"] if c["name"] == "state_dir")["ok"]
    assert sentinel.read_bytes() == b"operator-owned"
    assert list(tmp_path.iterdir()) == [sentinel]
