"""Host helper lifecycle with synthetic accounts/services; never runs privileged commands."""
import importlib.util
import os
from pathlib import Path
import socket
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/tunnel_host.py"
if not SOURCE.exists():
    SOURCE = ROOT / "extras/pond-tunnel/tunnel_host.py"
spec = importlib.util.spec_from_file_location("tunnel_host", SOURCE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class FakeHost:
    def __init__(self):
        self.accounts = {}
        self.listed = []
        self.live = []
        self.events = []
        self.fail_create = False
        self.fail_remove_service = False

    def ssh_check(self):
        self.events.append("sshd-check")

    def account(self, name):
        return self.accounts.get(name)

    def create(self, name, home, marker):
        self.events.append("create")
        self.accounts[name] = {"name": name, "uid": os.getuid(), "gid": os.getgid(),
                              "home": str(home), "shell": "/usr/sbin/nologin", "comment": marker,
                              "groups": [os.getgid()], "group_name": name}
        if self.fail_create:
            raise RuntimeError("synthetic post-create failure")

    def delete(self, identity):
        self.events.append("delete")
        del self.accounts[identity["name"]]

    def cleanup_group(self, identity):
        self.events.append("group-cleanup")

    def processes(self, uid):
        return self.live

    def services(self):
        return self.listed

    def guarded_removal_supported(self):
        self.events.append("guard-check")

    def remove_service(self, expected):
        if self.fail_remove_service:
            raise RuntimeError("synthetic authority failure")
        assert expected in self.listed
        self.events.append("service-remove")
        self.listed.remove(expected)


@pytest.fixture
def fixture(tmp_path):
    host = FakeHost()
    home = tmp_path / "account-home"
    source = tmp_path / "authorized_keys"
    source.write_bytes(b"ssh-ed25519 AQ== unrelated\nssh-ed25519 Ag== fixture-key\n")
    source.chmod(0o600)
    ledger = tool.Receipt(tmp_path / "receipt")
    args = dict(account="fixture", home=home, source=source, comment="fixture-key", box="192.0.2.1",
                control_plane="198.51.100.7:18503")
    return host, ledger, args


def setup(fixture):
    host, ledger, args = fixture
    with ledger.lock():
        tool.setup(host, ledger, **args)
    return host, ledger, args


def test_setup_update_teardown_preserves_unrelated_keys_and_backup(fixture):
    host, ledger, args = setup(fixture)
    backup = args["source"].with_name("authorized_keys.bak-existing")
    backup.write_bytes(b"ssh-ed25519 Ag== fixture-key\noperator-history\n")
    assert args["source"].read_bytes() == b"ssh-ed25519 AQ== unrelated\n"
    with ledger.lock():
        tool.setup(host, ledger, **{**args, "box": "192.0.2.2"})
        assert 'from="192.0.2.2"' in (args["home"] / ".ssh/authorized_keys").read_text()
        assert tool.teardown(host, ledger)["status"] == "removed"
        assert tool.teardown(host, ledger)["status"] == "already_removed"
    assert backup.read_bytes().endswith(b"operator-history\n")
    assert args["source"].read_bytes() == b"ssh-ed25519 AQ== unrelated\n"
    assert not args["home"].exists()


def test_existing_account_is_not_implicitly_adopted(fixture):
    host, ledger, args = fixture
    host.accounts["fixture"] = {"foreign": True}
    original = args["source"].read_bytes()
    with ledger.lock(), pytest.raises(ValueError, match="explicit adoption"):
        tool.setup(host, ledger, **args)
    assert "create" not in host.events
    assert args["source"].read_bytes() == original


def test_additional_home_files_or_changed_identity_refuse_before_removal(fixture):
    host, ledger, args = setup(fixture)
    unowned = args["home"] / "operator-notes"
    unowned.write_text("keep")
    with ledger.lock(), pytest.raises(ValueError, match="unowned files"):
        tool.teardown(host, ledger)
    assert unowned.read_text() == "keep" and "delete" not in host.events
    unowned.unlink()
    host.accounts["fixture"]["uid"] += 1
    with ledger.lock(), pytest.raises(ValueError, match="identity"):
        tool.teardown(host, ledger)
    assert "delete" not in host.events


def test_changed_key_or_live_account_processes_refuse(fixture):
    host, ledger, args = setup(fixture)
    host.live = ["synthetic-process"]
    with ledger.lock(), pytest.raises(ValueError, match="close its tunnel"):
        tool.teardown(host, ledger)
    host.live = []
    keys = args["home"] / ".ssh/authorized_keys"
    keys.write_text("operator replacement\n")
    with ledger.lock(), pytest.raises(ValueError, match="key state changed"):
        tool.teardown(host, ledger)
    assert keys.read_text() == "operator replacement\n" and "delete" not in host.events


def test_created_account_after_interrupted_call_is_verified_without_replay(fixture):
    host, ledger, args = fixture
    host.fail_create = True
    with ledger.lock(), pytest.raises(RuntimeError):
        tool.setup(host, ledger, **args)
    assert ledger.load()["phase"] == "creating-account"
    host.fail_create = False
    with ledger.lock():
        tool.setup(host, ledger, **args)
    assert host.events.count("create") == 1
    assert ledger.load()["phase"] == "active"


def test_service_cleanup_only_after_explicit_adoption_and_exact_match(fixture):
    host, ledger, args = setup(fixture)
    owned = {"name": "devbox", "client_id": "synthetic", "url": "http://127.0.0.1:18520/mcp",
             "ponds": ["pond"], "ping_timeout": 1.0}
    unrelated = {**owned, "name": "unrelated", "client_id": "other"}
    host.listed = [owned, unrelated]
    with ledger.lock():
        tool.claim_services(host, ledger, ["devbox"])
    host.listed[0] = {**owned, "client_id": "replacement"}
    with ledger.lock(), pytest.raises(ValueError, match="definition changed"):
        tool.teardown(host, ledger)
    assert "service-remove" not in host.events and "delete" not in host.events
    host.listed[0] = owned
    with ledger.lock():
        tool.teardown(host, ledger)
    assert host.listed == [unrelated]


def test_failed_service_cleanup_does_not_continue_to_account_removal(fixture):
    host, ledger, args = setup(fixture)
    host.listed = [{"name": "devbox", "client_id": "synthetic"}]
    with ledger.lock():
        tool.claim_services(host, ledger, ["devbox"])
    host.fail_remove_service = True
    with ledger.lock(), pytest.raises(RuntimeError):
        tool.teardown(host, ledger)
    assert "delete" not in host.events
    assert ledger.load()["phase"] == "removing"
    host.fail_remove_service = False
    with ledger.lock():
        tool.teardown(host, ledger)
    assert not host.accounts


def test_post_delete_record_failure_resumes_without_deleting_new_account(fixture, monkeypatch):
    host, ledger, args = setup(fixture)
    save = ledger.save
    def fail_after_delete(data):
        if data["phase"] == "removed":
            raise OSError("synthetic record failure")
        save(data)
    monkeypatch.setattr(ledger, "save", fail_after_delete)
    with ledger.lock(), pytest.raises(OSError):
        tool.teardown(host, ledger)
    assert not host.accounts and ledger.load()["phase"] == "removing"
    monkeypatch.setattr(ledger, "save", save)
    args["home"].symlink_to(args["home"].with_name("missing-target"))
    with ledger.lock(), pytest.raises(ValueError, match="cleanup is unconfirmed"):
        tool.teardown(host, ledger)
    assert ledger.load()["phase"] == "removing"
    args["home"].unlink()
    with ledger.lock():
        assert tool.teardown(host, ledger)["status"] == "removed"
    assert host.events.count("delete") == 1


def test_source_restoration_is_explicit_and_preserves_later_keys(fixture):
    host, ledger, args = setup(fixture)
    with args["source"].open("ab") as stream:
        stream.write(b"ssh-ed25519 Aw== later-unrelated\n")
    with ledger.lock():
        tool.teardown(host, ledger, restore_source=True)
    raw = args["source"].read_bytes()
    assert b"unrelated" in raw and b"later-unrelated" in raw and b"fixture-key" in raw


def test_foreign_receipt_and_duplicate_markers_refuse(fixture):
    host, ledger, args = fixture
    with args["source"].open("ab") as stream:
        stream.write(b"ssh-ed25519 BA== fixture-key\n")
    with ledger.lock(), pytest.raises(ValueError, match="exactly one"):
        tool.setup(host, ledger, **args)
    assert "create" not in host.events
    with ledger.lock():
        ledger.save({"schema": 2, "owner": tool.OWNER, "machine": socket.gethostname()})
        with pytest.raises(ValueError, match="unsupported"):
            ledger.load()


def test_control_plane_is_explicit_and_canonical():
    assert tool.control_plane_target("198.51.100.7:18503") == "198.51.100.7:18503"
    assert tool.control_plane_target("[2001:db8::1]:443") == "[2001:db8::1]:443"
    line = tool.restricted_key("ssh-ed25519 AAAA", "192.0.2.1", "c", "198.51.100.7:18503")
    assert 'permitopen="198.51.100.7:18503"' in line
    for bad in ("host.example:18503", "198.51.100.7", "198.51.100.7:0", "198.51.100.7:x"):
        with pytest.raises(ValueError):
            tool.control_plane_target(bad)


# --- the Crocodile's admin executable -----------------------------------------

def executable(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho '[]'\n")
    path.chmod(mode)
    return path


def test_no_retired_layout_is_named():
    assert "boring-crocodile-deb" not in SOURCE.read_text()


def test_default_admin_is_the_maintained_layout(tmp_path):
    assert tool.default_admin(tmp_path / "absent-homes.json") == Path(
        "/opt/boring-crocodile/current/bin/boring-crocodile-admin")


def test_a_recorded_home_is_used(tmp_path):
    homes = tmp_path / "homes.json"
    homes.write_text('{"boring-crocodile": "/srv/croc", "boring-canvas": "/srv/canvas"}')
    assert tool.default_admin(homes) == Path("/srv/croc/current/bin/boring-crocodile-admin")
    homes.write_text('{"boring-crocodile": "relative"}')
    with pytest.raises(ValueError, match="absolute HOME"):
        tool.default_admin(homes)


def test_a_new_layout_admin_through_current_is_accepted(tmp_path):
    release = executable(tmp_path / "home/releases/1.0/bin/boring-crocodile-admin").parent.parent
    (tmp_path / "home/current").symlink_to(release)
    given = tmp_path / "home/current/bin/boring-crocodile-admin"
    assert tool.checked_admin(given) == release.resolve() / "bin/boring-crocodile-admin"


@pytest.mark.parametrize("problem", ["missing", "relative", "not-executable", "group-writable", "directory"])
def test_an_unusable_admin_is_refused_clearly(tmp_path, problem):
    path = tmp_path / "bin/boring-crocodile-admin"
    if problem == "relative":
        path = Path("bin/boring-crocodile-admin")
    elif problem == "not-executable":
        executable(path, 0o644)
    elif problem == "group-writable":
        executable(path, 0o775)
    elif problem == "directory":
        path.mkdir(parents=True)
    with pytest.raises(ValueError) as refused:
        tool.checked_admin(path)
    if problem == "missing":
        assert "--admin" in str(refused.value) and str(path) in str(refused.value)


def test_host_resolves_admin_only_when_service_authority_is_needed(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(tool.Host, "run", lambda self, *args: ran.append(args) or "[]")
    missing = tool.Host(tmp_path / "absent/boring-crocodile-admin")  # constructing it needs nothing
    with pytest.raises(ValueError, match="no boring-crocodile-admin"):
        missing.services()
    assert ran == []  # refused before anything ran
    admin = executable(tmp_path / "bin/boring-crocodile-admin")
    assert tool.Host(admin).services() == []
    assert ran == [(str(admin), "service-list")]
