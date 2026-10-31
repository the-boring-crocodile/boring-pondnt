"""Form adapter regression tests with the real executor/setter and synthetic browser responses."""
from contextlib import contextmanager
import threading

import pytest

from boring_pondnt.d365web_client import BROWSER, Config, D365WebError, WebClient
from boring_pondnt.d365whsapp_backends import FormBackend
from boring_pondnt.d365whsapp_server import create_server
from boring_pondnt.d365web_whsform import page_from_form
from test_d365whsapp_server import call, FORM_S1


@pytest.fixture
def web():
    client = WebClient(Config(url="https://fixture.invalid"), budget=0.1)
    try:
        yield client
    finally:
        client.shutdown()


@pytest.mark.parametrize("fails", [False, True])
def test_only_owning_screen_collects_delayed_step(web, tmp_path, fails):
    release = threading.Event()
    invoked = []
    page = page_from_form(FORM_S1)

    def delayed(*args):
        assert release.wait(5)
        if fails:
            raise D365WebError("browser", "synthetic failure; outcome unknown")
        return {**page, "title": "Original completion"}

    backend = FormBackend(web)
    backend._op_submit = delayed
    backend._op_screen = lambda worker: invoked.append(worker) or page
    server = create_server(backend, tmp_path, log_calls=True)
    try:
        error, first = call(server, "whs_submit", {"worker": "w1", "press": "OK"})
        assert error and first["code"] == "step_running"
        pending = web.runs.get(BROWSER).future
        # Another worker cannot consume even a still-running operation.
        error, busy = call(server, "whs_screen", {"worker": "W2"})
        assert error and busy["code"] == "busy"
        error, running = call(server, "whs_screen", {"worker": "W1"})
        assert error and running["code"] == "step_running"
        release.set()
        try:
            pending.result(timeout=5)
        except D365WebError:
            assert fails
        # Nor a completed one. New effects by either worker must wait for collection.
        for worker, tool in [("W2", "whs_screen"), ("W2", "whs_submit"), ("W1", "whs_submit")]:
            error, busy = call(server, tool, {"worker": worker})
            assert error and busy["code"] == "busy"
            assert web.runs.get(BROWSER).future is pending
        error, result = call(server, "whs_screen", {"worker": "W1"})
        assert error == fails
        if fails:
            assert result["code"] == "browser" and "outcome unknown" in result["message"]
            assert "synthetic failure" in (tmp_path / "whs-steps/steps-W1.log").read_text()
        else:
            assert result["title"] == "Original completion"
        assert invoked == []  # collection reports the original result, without rereading/replacing
        error, result = call(server, "whs_screen", {"worker": "W2"})
        assert not error and invoked == ["W2"]
    finally:
        release.set()


def setting_fixture(web, backend, outcome):
    typed, pressed = [], []
    warning = {"severity": "warning", "text": "The batch was rejected"}

    class Page:
        def click(self, *args): pass
        def key(self, *args): pass
        def type_text(self, text): typed.append(text)

    page = Page()

    @contextmanager
    def tab(worker, create=False):
        yield page

    backend._tab = tab
    web._ensure = lambda: page
    web._locate = lambda *args: {"x": 1, "y": 1, "value": "", "role": "Input"}
    web._wait_idle = lambda *args: True

    def js(op, *args):
        if op == "messages":
            return [warning] if typed and outcome == "warning" else []
        if op == "locate":
            return {"value": "" if outcome in ("warning", "differs") else typed[-1]}
        if op == "whsform":
            return FORM_S1
        raise AssertionError(op)

    web._js = js
    web._after_step = lambda before, **extra: {
        "scope": "dialog", "form": "WHSWorkExecute",
        "new_messages": [warning] if outcome == "warning" else [], **extra}
    backend._press = pressed.append
    backend._page = lambda: page_from_form(FORM_S1)
    backend._walk_out = lambda: None
    return typed, pressed


@pytest.mark.parametrize("outcome", ["warning", "differs", "valid"])
@pytest.mark.parametrize("budget", [0.1, 2])
def test_submit_respects_actual_setter_result(web, tmp_path, outcome, budget):
    web.budget = budget
    backend = FormBackend(web)
    typed, pressed = setting_fixture(web, backend, outcome)
    server = create_server(backend, tmp_path, log_calls=True)
    error, result = call(server, "whs_submit", {
        "worker": "W1", "values": {"Batch": "B1", "Quantity": "7"}, "press": "OK"})
    # The real setter may take longer than this test's public call budget.
    if error and result["code"] == "step_running":
        web.runs.get(BROWSER).future.exception(timeout=5)
        error, result = call(server, "whs_screen", {"worker": "W1"})
    if outcome == "valid":
        assert not error and pressed == ["OK"]
        assert typed == ["B1", "7"]
    else:
        assert error and result["code"] == "set_incomplete"
        assert pressed == []
        assert result["details"]["applied"][0]["differs"] is True
        if outcome == "warning":
            assert typed == ["B1"]
            assert result["details"]["stopped_at"] == "Batch"
            assert result["details"]["new_messages"][0]["severity"] == "warning"
        else:
            assert typed == ["B1", "7"]


def test_login_stops_on_rejected_values_and_masks_password(web, tmp_path):
    backend = FormBackend(web)
    typed, pressed = setting_fixture(web, backend, "differs")
    server = create_server(backend, tmp_path, log_calls=True)
    error, result = call(server, "whs_login", {"worker": "W1", "password": "s3cret"})
    if error and result["code"] == "step_running":
        web.runs.get(BROWSER).future.exception(timeout=5)
        error, result = call(server, "whs_screen", {"worker": "W1"})
    assert error and result["code"] == "set_incomplete"
    assert typed == ["W1", "s3cret"] and pressed == []
    assert result["details"]["applied"][1]["value"] == "***"
    assert "s3cret" not in str(result)
    assert "s3cret" not in (tmp_path / "whs-steps/steps-W1.log").read_text()


def test_valid_login_presses_once(web, tmp_path):
    web.budget = 2
    backend = FormBackend(web)
    typed, pressed = setting_fixture(web, backend, "valid")
    server = create_server(backend, tmp_path, log_calls=True)
    error, result = call(server, "whs_login", {"worker": "W1", "password": "s3cret"})
    assert not error and pressed == ["Login"]
    assert typed == ["W1", "s3cret"]


def test_form_public_page_masks_secret_fields():
    from boring_pondnt.d365web_whsform import public
    raw = {"page": "Login", "fields": {"UserId": "W1", "Password": "s3cret", "Pin": "42"}}
    assert public(raw)["fields"] == {"UserId": "W1", "Password": "***", "Pin": "***"}
    assert raw["fields"]["Password"] == "s3cret"  # browser state is not mutated
