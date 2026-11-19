"""Form/WHS forwarding preserves raising-site decisions and uncertain effects."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from boring_pondnt import d365whsapp_backends as module
from boring_pondnt.d365web_browser import BrowserError
from boring_pondnt.d365whsapp_backends import FormBackend
from boring_pondnt.d365whsapp_client import WorkerSession, WhsError
from boring_pondnt.d365whsapp_protocol import parse_page
from boring_pondnt.d365whsapp_server import create_server, _outcome
from boring_pondnt.d365_entra import read_dpapi_secret
from boring_pondnt.whs_logoff import LookupPending
from test_action_outcomes import rig
from test_d365whsapp_server import call
from test_d365whsapp_client import FakeAos, client as app_client, sample


FORM_CASES = [
    ("tab_missing", "no_session", False),
    ("tab_gone", "no_session", True),
    ("page_missing", "not_found", False),
    ("walk_pending", "retry_later", False),
    ("walk_no_exit", "stuck", False),
    ("walk_repeated", "stuck", True),
    ("walk_cap", "stuck", True),
    ("set_incomplete", "set_incomplete", False),
    ("login_pending", "retry_later", False),
    ("login_cleanup", "conflict", True),
]


def form_case(web, page, monkeypatch, case):
    web.browser = SimpleNamespace(conn=SimpleNamespace(closed=False))
    web.runs = SimpleNamespace(get=lambda *a: None)
    web._after_step = lambda before, **kw: {"scope": "dialog", "form": "WHSWorkExecute", **kw}
    web._locate = lambda *a: {"control": "Back", "x": 1, "y": 2}
    backend = FormBackend(web)
    backend.tabs_browser = web.browser
    backend._logoff_worker = "W1"
    menu = {"page": "Menu", "buttons": ["Back"], "_button_text": {"Back": "Back"}}
    backend._page = lambda: menu
    items = SimpleNamespace(names=(), stuck=lambda *a: "no exit was taken")
    backend.logoff_items = SimpleNamespace(get=lambda *a, **kw: items, buttons=(),
                                          notes=lambda *a: [], observe=lambda *a: None)

    if case.startswith("tab_"):
        if case == "tab_gone":
            backend.tabs["W1"] = page
            def gone(*a):
                page.events.append(("bring-to-front",))
                raise BrowserError("closed", "tab is gone")
            page.send = gone
        def work():
            with backend._tab("W1"):
                raise AssertionError("missing tab must not be yielded")
        return backend, work
    if case == "page_missing":
        web._js = lambda *a: None
        return backend, lambda: FormBackend._page(backend)
    if case in {"walk_pending", "login_pending"}:
        def pending(*a, **kw): raise LookupPending(0)
        backend.logoff_items.get = pending
        if case == "login_pending":
            @contextmanager
            def tab(*a, **kw): yield page
            backend._tab = tab
            web._js = lambda op, *a: {"controls": []} if op == "whsform" else {}
            return backend, lambda: backend._login_steps("W1", "pw", None, resume=False)
        return backend, backend._walk_out
    if case.startswith("walk_"):
        if case == "walk_no_exit":
            menu["buttons"] = ["Continue"]
            menu["_button_text"] = {"Continue": "Continue"}
        if case == "walk_cap":
            monkeypatch.setattr(module, "LOGOFF_STEPS_MAX", 1)
        return backend, backend._walk_out
    if case == "set_incomplete":
        web._op_set = lambda *a: {"applied": []}  # a setter that sent nothing
        return backend, lambda: backend._set([("X", "value")])
    if case == "login_cleanup":
        def login_steps(worker, *a, **kw):
            backend.tabs[worker] = page
            web._locate = lambda *a: {"enabled": False}
            return web._op_press("disabled", 0)
        backend._login_steps = login_steps
        return backend, lambda: backend._op_login("W1", "pw", None)
    raise AssertionError(case)


@pytest.mark.parametrize("prefix", [False, True], ids=["untouched", "after-input"])
@pytest.mark.parametrize("case,code,acts", FORM_CASES, ids=[c[0] for c in FORM_CASES])
def test_form_raising_sites_pass_through_whs(rig, monkeypatch, tmp_path, prefix, case, code, acts):
    web, page = rig
    backend, operation = form_case(web, page, monkeypatch, case)
    def work(*args):
        if prefix:
            web._op_key("Tab")
        return operation()
    backend._op_submit = work
    error, data = call(create_server(backend, tmp_path), "whs_submit", {"worker": "W1"})
    assert error and data["code"] == code
    assert data["status"] == ("failed" if prefix or acts else "refused")
    if data["status"] == "failed":
        assert data["effects"] == "unknown" and page.events
    else:
        assert "effects" not in data and page.events == []
    if "page" in data:
        assert isinstance(data["page"], dict)  # never mistaken for protocol Page


def test_refused_form_logoff_retains_its_cached_page(rig, monkeypatch, tmp_path):
    web, page = rig
    backend, operation = form_case(web, page, monkeypatch, "walk_no_exit")
    backend.pages["W1"] = original = {"page": "Menu", "title": "observed"}
    backend._op_logoff = lambda *a: operation()
    error, data = call(create_server(backend, tmp_path), "whs_logoff", {"worker": "W1"})
    assert error and data["status"] == "refused" and data["code"] == "stuck"
    assert backend.pages["W1"] is original and page.events == []


def test_app_logoff_failure_cannot_hide_local_session_removal(monkeypatch):
    fake = FakeAos()
    app = app_client(fake)
    app.sessions["W1"] = WorkerSession("W1", "usmf", "device",
                                      page=parse_page(sample("poreceive-qty-tab.xml")))
    app.logoff_items.get = lambda *a, **kw: SimpleNamespace(names=(), stuck=lambda *a: "no exit")
    monkeypatch.setattr("boring_pondnt.d365_entra.sys.platform", "linux")
    app.tokens.secret = lambda: read_dpapi_secret("missing")
    with pytest.raises(WhsError) as caught:
        app.logoff("W1")
    data = _outcome(caught.value).as_data()
    assert data["status"] == "failed" and data["code"] == "secret_unavailable"
    assert data["effects"] == "unknown"
    assert "W1" not in app.sessions and not fake.requests
