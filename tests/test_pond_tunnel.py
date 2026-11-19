"""Desktop tunnel inputs and lifecycle with fakes; no SSH or native processes."""
import copy
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

from boring_pondnt import pond_tunnel as tunnel


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("id", "secret", "ssh-key", "known-hosts"):
        (tmp_path / name).write_text("synthetic")
    data = {"box": "devbox", "ssh_host": "tunnel@example.test", "identity_file": "ssh-key",
            "known_hosts_file": "known-hosts", "crocodile_url": "https://pond.example.test",
            "servers": {"core": {"client_id_file": "id", "secret_file": "secret"}}}
    file = tmp_path / "settings.json"
    file.write_text(json.dumps(data))
    monkeypatch.setattr(tunnel, "check_ports", lambda ports: None)
    return file, data


def save(settings, data):
    file, _ = settings
    file.write_text(json.dumps(data))
    return file


def test_second_box_names_ports_and_canonical_urls(settings):
    file, data = settings
    data["box"] = "second-box"
    data["servers"]["core"]["port"] = 18620
    data["servers"]["d365"] = {"port": 18622, "client_id_file": "id", "secret_file": "secret"}
    prepared = tunnel.plan(save(settings, data), "bundled-python")
    assert [s["name"] for s in prepared["servers"]] == ["second-box", "second-box-d365"]
    assert [s["config"]["url"] for s in prepared["servers"]] == [
        "http://127.0.0.1:18620/mcp", "http://127.0.0.1:18622/mcp"]
    assert "127.0.0.1:18620:127.0.0.1:18620" in prepared["ssh"]
    assert "127.0.0.1:18622:127.0.0.1:18622" in prepared["ssh"]
    assert "-L" not in prepared["ssh"]
    assert prepared["ssh"][-1] == "tunnel@example.test"
    assert "ExitOnForwardFailure=yes" in prepared["ssh"]
    assert "BatchMode=yes" in prepared["ssh"]
    assert not (file.parent / "servers").exists()


@pytest.mark.parametrize("bad", ["duplicate", "missing-secret", "unknown-server", "bad-access"])
def test_invalid_selected_input_has_no_state_effects(settings, bad):
    file, data = settings
    if bad == "duplicate":
        data["servers"]["d365"] = {**data["servers"]["core"], "port": 18530}
    elif bad == "missing-secret":
        data["servers"]["core"]["secret_file"] = "missing"
    elif bad == "unknown-server":
        data["servers"]["knowledge"] = data["servers"]["core"]
    else:
        data["access"] = [{"subject": "pond", "role": "admin"}]
    with pytest.raises(ValueError):
        tunnel.plan(save(settings, data))
    assert not (file.parent / "servers").exists()


def test_operator_can_choose_a_free_port_in_the_normal_default_range(settings):
    file, data = settings
    data["servers"]["core"]["port"] = 18513
    prepared = tunnel.plan(save(settings, data))
    assert prepared["servers"][0]["config"]["url"] == "http://127.0.0.1:18513/mcp"


def test_occupied_port_names_clash(monkeypatch):
    # Exercise the real binding preflight, holding the socket throughout.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match=f"other-box.*{port}.*already in use"):
            tunnel.check_ports({port: "other-box"})


def test_web_copy_has_its_own_profile_and_debug_port(settings):
    file, data = settings
    data["servers"] = {"d365web": {"client_id_file": "id", "secret_file": "secret",
                                 "web": {"url": "https://aos.example.test", "profile": "pond-tunnel",
                                         "debug_port": 9343}}}
    server = tunnel.plan(save(settings, data))["servers"][0]
    assert server["web"]["profiles_dir"] == str(file.parent / "servers/devbox-d365web/edge")
    assert server["web"]["debug_port"] == 9343
    assert "--d365web" in server["argv"]


class Child:
    def __init__(self, exited=False):
        self.returncode = 1 if exited else None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -1

    def wait(self, timeout):
        return self.returncode


def test_ssh_reconnect_keeps_servers_and_shutdown_stops_current_children(settings):
    file, _ = settings
    prepared = tunnel.plan(file)
    starts = []

    def popen(argv, **kwargs):
        assert kwargs["close_fds"] is True
        ssh = argv[0] == "ssh.exe"
        child = Child(exited=ssh and len(starts) == 1)
        starts.append((copy.copy(argv), child))
        return child

    def sleep(_):
        if len(starts) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        tunnel.run(prepared, popen=popen, sleep=sleep)
    assert len(starts) == 3  # one server, two SSH connections
    assert starts[0][0] == prepared["servers"][0]["argv"]
    assert starts[1][0] == starts[2][0] == prepared["ssh"]
    assert starts[0][1].terminated and starts[2][1].terminated


def test_server_exit_ends_group_without_restarting_it(settings):
    file, _ = settings
    prepared = tunnel.plan(file)
    child = Child(exited=True)
    with pytest.raises(RuntimeError, match="devbox: server exited"):
        tunnel.run(prepared, popen=lambda *a, **kw: child, sleep=lambda _: None)


def test_containment_failure_never_starts_servers(settings, monkeypatch):
    import ctypes
    file, _ = settings
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: True)), raising=False)
    monkeypatch.setattr(sys, "argv", ["tunnel", "--settings", str(file)])
    monkeypatch.setattr(tunnel, "contain_window", lambda: (_ for _ in ()).throw(OSError("synthetic job failure")))
    monkeypatch.setattr(tunnel, "run", lambda *_: pytest.fail("started without containment"))
    assert tunnel.main() == 1
    assert not (file.parent / "servers").exists()


def test_window_job_is_noninheritable_and_contains_current_process(monkeypatch):
    events = []
    api = SimpleNamespace(SetHandleInformation=lambda *a: events.append(("inherit", *a)),
                          GetCurrentProcess=lambda: "self", CloseHandle=lambda *a: events.append(("close", *a)))
    job = SimpleNamespace(CreateJobObject=lambda *a: "job", JobObjectExtendedLimitInformation=9,
                          JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
                          QueryInformationJobObject=lambda *a: {"BasicLimitInformation": {}},
                          SetInformationJobObject=lambda *a: events.append(("limits", a[-1])),
                          AssignProcessToJobObject=lambda *a: events.append(("assign", *a)))
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32job", job)
    monkeypatch.setattr(tunnel, "_WINDOW_JOB", None)
    tunnel.contain_window()
    assert events == [("limits", {"BasicLimitInformation": {"LimitFlags": 0x2000}}),
                      ("inherit", "job", 1, 0), ("assign", "job", "self")]
    assert tunnel._WINDOW_JOB == "job"


def test_crocodile_config_uses_one_authority_and_the_real_owner_parser(settings):
    from boring_service_kit.http import ServiceConfig
    file, _ = settings
    server = tunnel.plan(file)["servers"][0]
    config = server["config"]
    assert config == {
        "url": "http://127.0.0.1:18530/mcp", "mode": "crocodile",
        "crocodile": {"url": "https://pond.example.test", "client_id": "synthetic",
                      "secret_file": str(file.parent / "secret")}}
    # The generated config can actually start against the current kit, read-only here.
    candidate = file.parent / "generated.json"
    candidate.write_text(json.dumps(config))
    info = ServiceConfig.inspect(candidate)
    assert info.authority_url == "https://pond.example.test"
    assert (info.bind, info.port) == ("127.0.0.1", 18530)


@pytest.mark.parametrize(("field", "value", "replacement"), [
    ("access", [{"subject": "pond", "role": "writer"}], "service-grants"),
    ("control_plane", "192.0.2.1:18503", "crocodile_url"),
    ("control_port", 18503, "crocodile_url"),
])
def test_retired_settings_name_their_replacement_without_effects(settings, field, value, replacement):
    file, data = settings
    data[field] = value
    with pytest.raises(ValueError, match=field + ".*" + replacement):
        tunnel.plan(save(settings, data))
    assert not (file.parent / "servers").exists()


@pytest.mark.parametrize("authority", ["http://192.0.2.1:18503", "https://user:secret@pond.example.test",
                                      "https://pond.example.test?token=synthetic", "not-a-url"])
def test_bad_authority_is_refused_before_state_or_processes(settings, authority):
    file, data = settings
    data["crocodile_url"] = authority
    with pytest.raises(ValueError, match="Crocodile|URL|HTTPS"):
        tunnel.plan(save(settings, data))
    assert not (file.parent / "servers").exists()
