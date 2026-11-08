"""Tests for the shared Entra client and HTTP transport."""
import io
import json
import socket
import urllib.error
import urllib.parse

import pytest

from boring_pondnt import d365_entra as entra
from boring_pondnt.d365whsapp_client import Config, WhsClient, WhsError


def test_scope_and_token_cache_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(entra.time, "monotonic", lambda: now[0])
    requests = []

    def transport(url, headers, body, timeout):
        requests.append((url, headers, urllib.parse.parse_qs(body.decode()), timeout))
        return entra.Response(200, json.dumps({"access_token": f"token-{len(requests)}", "expires_in": 3600}))

    tokens = entra.TokenSource("tenant", "client", "https://resource.example/.default",
                               transport, lambda: "secret")
    assert tokens.get() == "token-1"
    now[0] = 4479
    assert tokens.get() == "token-1"
    now[0] = 4480
    assert tokens.get() == "token-2"
    url, headers, body, timeout = requests[0]
    assert url == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert body == {"grant_type": ["client_credentials"], "client_id": ["client"],
                    "client_secret": ["secret"], "scope": ["https://resource.example/.default"]}
    assert timeout == 30.0


@pytest.mark.parametrize("failure", [socket.timeout("timeout"), OSError("network")])
def test_token_network_failure_is_reported(failure):
    def transport(*args):
        raise failure
    tokens = entra.TokenSource("tenant", "client", "scope", transport, lambda: "secret")
    with pytest.raises(entra.EntraError) as raised:
        tokens.get()
    assert raised.value.code == "token_failed"
    assert "Entra token request failed" in raised.value.message


@pytest.mark.parametrize("body", [
    "not JSON: private reply text",
    "null",
    "[]",
    "{}",
    '{"access_token": 12}',
    '{"access_token": ""}',
    '{"access_token": " "}',
])
def test_unusable_token_reply_is_reported_without_reply_text(body):
    tokens = entra.TokenSource("tenant", "client", "scope",
                               lambda *args: entra.Response(200, body), lambda: "secret")
    with pytest.raises(entra.EntraError) as raised:
        tokens.get()
    assert raised.value.code == "token_failed"
    assert raised.value.message == "Entra returned an unusable token reply"


@pytest.mark.parametrize("expires_in", [None, "", False, -1, "NaN", "Infinity", 10**400])
def test_invalid_token_expiry_is_reported_without_caching(expires_in):
    replies = iter([
        {"access_token": "rejected-token", "expires_in": expires_in},
        {"access_token": "usable-token"},
    ])
    tokens = entra.TokenSource("tenant", "client", "scope",
                               lambda *args: entra.Response(200, json.dumps(next(replies))),
                               lambda: "secret")
    with pytest.raises(entra.EntraError) as raised:
        tokens.get()
    assert raised.value.code == "token_failed"
    assert raised.value.message == "Entra returned an unusable token reply"
    assert tokens.get() == "usable-token"
    assert tokens.get() == "usable-token"


def test_token_http_failure_and_secret_failure_map_to_whs():
    config = Config("https://aos.example", "tenant", "client")
    def http_error(*args):
        return entra.Response(401, "denied")
    client = WhsClient(config, transport=http_error, secret=lambda: "secret")
    with pytest.raises(WhsError) as raised:
        client.call("method", {})
    assert raised.value.code == "token_failed"
    assert raised.value.message == "Entra token request: HTTP 401"

    def unavailable():
        raise entra.EntraError("secret_unavailable", "cannot decrypt")
    client = WhsClient(config, transport=http_error, secret=unavailable)
    with pytest.raises(WhsError) as raised:
        client.call("method", {})
    assert (raised.value.code, raised.value.message) == ("secret_unavailable", "cannot decrypt")


def test_whs_keeps_client_id_scope():
    requests = []
    def transport(url, headers, body, timeout):
        requests.append(urllib.parse.parse_qs(body.decode()))
        return entra.Response(401, "denied")
    client = WhsClient(Config("https://aos.example", "tenant", "client"),
                       transport=transport, secret=lambda: "secret")
    with pytest.raises(WhsError):
        client.call("method", {})
    assert requests[0]["scope"] == ["client/.default"]


@pytest.mark.parametrize(("method", "body"), [("GET", None), ("POST", b"data"),
                                             ("PATCH", b"data"), ("DELETE", None)])
def test_urllib_transport_sends_method(monkeypatch, method, body):
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"ok"

    def urlopen(request, timeout):
        assert request.get_method() == method
        assert request.data == body
        assert request.get_header("X-test") == "yes"
        assert timeout == 4
        return Reply()

    monkeypatch.setattr(entra.urllib.request, "urlopen", urlopen)
    assert entra.urllib_transport("https://example.test", {"X-Test": "yes"}, body, 4, method=method, allow_redirects=True) == entra.Response(200, "ok")


def test_urllib_transport_preserves_http_response_and_raises_network(monkeypatch):
    def http_error(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))
    monkeypatch.setattr(entra.urllib.request, "urlopen", http_error)
    assert entra.urllib_transport("https://example.test", {}, None, 4, method="GET", allow_redirects=True) == entra.Response(403, "no")

    def network_error(request, timeout):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(entra.urllib.request, "urlopen", network_error)
    with pytest.raises(urllib.error.URLError):
        entra.urllib_transport("https://example.test", {}, None, 4, method="GET", allow_redirects=True)


def test_secret_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(entra.sys, "platform", "linux")
    with pytest.raises(entra.EntraError) as raised:
        entra.read_dpapi_secret("unused")
    assert raised.value.code == "secret_unavailable"


def test_urllib_transport_never_follows_a_redirect_by_default(monkeypatch):
    # urllib copies Authorization to the redirect target, another host included
    used = []
    class Opener:
        def open(self, request, timeout):
            used.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, io.BytesIO(b""))
    monkeypatch.setattr(entra.urllib.request, "build_opener", lambda *handlers: used.append(handlers) or Opener())
    monkeypatch.setattr(entra.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("urlopen follows redirects")))
    assert entra.urllib_transport("https://example.test", {"Authorization": "Bearer t"}, None, 4, method="GET").status == 302
    assert any(isinstance(h, entra._NoRedirect) for h in used[0])


def test_invalidate_drops_only_the_rejected_token(monkeypatch):
    monkeypatch.setattr('boring_pondnt.d365_entra.FRESH_TOKEN_SECONDS', 0.0)  # an old enough token
    issued = []

    def transport(url, headers, body, timeout):
        issued.append(f"t{len(issued) + 1}")
        return entra.Response(200, json.dumps({"access_token": issued[-1], "expires_in": 3600}))

    tokens = entra.TokenSource("tenant", "client", "scope", transport, lambda: "secret")
    assert tokens.get() == "t1" and tokens.get() == "t1"
    tokens.invalidate("other")  # already replaced by another caller: keep the current one
    assert tokens.get() == "t1"
    tokens.invalidate("t1")
    assert tokens.get() == "t2" and len(issued) == 2
