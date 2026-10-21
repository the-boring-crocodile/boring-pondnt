"""Finite session output, without a real D365 environment."""
import io
from types import SimpleNamespace

import pytest

from boring_pondnt import d365
from boring_service_kit.outcomes import Failed
from test_d365 import fake_ps  # noqa: F401  (pytest fixture)


def test_reader_limits_even_an_unterminated_line(monkeypatch):
    monkeypatch.setattr(d365, "SESSION_LINE_BYTES", 32)
    stream = io.BytesIO(b"x" * 10000)
    output = d365.SessionOutput()
    output.read(stream)
    assert stream.tell() == 33
    with pytest.raises(d365.SessionOutputError, match="line exceeds"):
        output.get(0)


def test_full_buffer_never_blocks_or_silently_drops_a_reply(monkeypatch):
    monkeypatch.setattr(d365, "SESSION_QUEUE_LINES", 2)
    output = d365.SessionOutput()
    output.read(io.BytesIO(b"one\ntwo\n@@RESULT {}\n"))
    assert output.lines.qsize() == 2
    with pytest.raises(d365.SessionOutputError, match="buffer exceeds"):
        output.get(0)


@pytest.mark.parametrize("payload", ["{", "[]", "null", '{"ok":1}', '{"value":0}'])
def test_malformed_protocol_is_a_bounded_error(payload):
    with pytest.raises(d365.SessionOutputError):
        d365.session_reply("@@RESULT " + payload, "@@RESULT ")


def fake_session(lines):
    session = d365.Session("unused")
    session.state = {"state": "ready"}
    session._process = SimpleNamespace(poll=lambda: None, stdin=io.BytesIO(), kill=lambda: None)
    iterator = iter(lines)
    session._lines = SimpleNamespace(get=lambda timeout: next(iterator), error=None)
    return session


def test_known_idle_overflow_refuses_before_sending_a_command():
    session = fake_session([])
    stream = session._process.stdin
    session._lines.error = "PowerShell output buffer exceeds the session limit"
    with pytest.raises(Failed) as error:
        session.call("must not run")
    assert error.value.code == "output_invalid"
    assert stream.getvalue() == b""
    assert session.state["state"] == "failed"


def test_noise_retains_only_a_clipped_tail():
    lines = ["x" * 10000 + str(i) for i in range(1000)]
    session = fake_session(lines + ['@@RESULT {"ok":false,"error":"synthetic"}'])
    with pytest.raises(Failed) as error:
        session.call("unused")
    assert error.value.code == "command_failed"
    assert len(error.value.details["noise"]) == d365.NOISE_LINES
    assert all(len(line) == d365.NOISE_LINE_CHARS for line in error.value.details["noise"])


def test_host_output_is_preserved_or_explicitly_refused(monkeypatch):
    monkeypatch.setattr(d365, "HOST_OUTPUT_CHARS", 10)
    result = '@@RESULT {"ok":true,"value":null}'
    assert fake_session(["hello", result]).call("unused", with_host=True) == (None, ["hello"])
    session = fake_session(["hello", "world", result])
    with pytest.raises(Failed) as error:
        session.call("unused", with_host=True)
    assert error.value.code == "output_invalid"
    assert session.state["state"] == "failed"


@pytest.mark.parametrize("script", ["Oversized-Line", "Malformed-Reply"])
def test_real_pipe_failure_settles_session_without_restart(fake_ps, script):  # noqa: F811
    session = d365.Session(fake_ps)
    session.start()
    try:
        session.ensure_ready(wait=15)
        process = session._process
        with pytest.raises(Failed) as error:
            session.call(script, timeout=5)
        assert error.value.code == "output_invalid"
        assert session.state["state"] == "failed"
        assert session._process is None
        process.wait(timeout=5)
    finally:
        session.close()


def test_malformed_ready_does_not_leave_loading_forever(fake_ps, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_PS_MODE", "bad_ready")
    session = d365.Session(fake_ps)
    session.start()
    try:
        with pytest.raises(Failed):
            session.ensure_ready(wait=15)
        assert session.state["state"] == "failed"
    finally:
        session.close()
