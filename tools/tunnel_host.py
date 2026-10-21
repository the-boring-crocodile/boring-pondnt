"""Optional tunnel host provisioning. A private receipt owns cleanup; no name-only deletion."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import grp
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import subprocess
import tempfile

OWNER = "pondnt-tunnel"
# The Crocodile's maintained layout (boring-deploy): HOME/current/bin/..., HOME by
# default /opt/boring-crocodile, another HOME recorded in boring-deploy's homes.json.
CROCODILE = "boring-crocodile"
CROCODILE_HOME = Path("/opt/boring-crocodile")
HOMES = Path("/etc/boring-pond/homes.json")


def default_admin(homes: Path = HOMES) -> Path:
    """boring-crocodile-admin of the installed Crocodile: its HOME from homes.json, else the default."""
    home = CROCODILE_HOME
    if homes.exists():
        known = json.loads(read_file(homes))
        if not isinstance(known, dict):
            raise ValueError(f"{homes} must map components to their HOME")
        if CROCODILE in known:
            home = Path(known[CROCODILE]) if isinstance(known[CROCODILE], str) else Path()
            if not home.is_absolute():
                raise ValueError(f"{homes} names no absolute HOME for {CROCODILE}")
    return home / "current" / "bin" / "boring-crocodile-admin"


def checked_admin(path: Path) -> Path:
    """The admin executable this root helper may run: present, executable, root's (or ours) alone."""
    if not path.is_absolute():
        raise ValueError("--admin must be an absolute path")
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError(f"no boring-crocodile-admin at {path}; install the Crocodile or pass --admin "
                         "with the installed executable") from None
    info = real.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        raise ValueError(f"{real} is not an executable file")
    if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
        raise ValueError(f"{real} must be owned by root and writable by nobody else")
    return real


def read_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("ordinary single-link file required")
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("file size exceeds limit")
        return raw


def write_file(path: Path, raw: bytes, expected: bytes | None, identity=None):
    """Compare before atomic publication; preserve existing ownership/mode/access ACLs."""
    current = read_file(path) if path.exists() or path.is_symlink() else None
    if current != expected:
        raise ValueError("file changed; refusing replacement")
    info = path.lstat() if current is not None else None
    fd, temporary = tempfile.mkstemp(prefix=".tunnel-new-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if info:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                for name in os.listxattr(path, follow_symlinks=False):
                    os.setxattr(stream.fileno(), name, os.getxattr(path, name, follow_symlinks=False))
            elif identity is not None:
                os.fchown(stream.fileno(), identity["uid"], identity["gid"])
                os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Cooperative helper calls hold the receipt lock. Other administrator edits still fail this check.
        if (read_file(path) if path.exists() or path.is_symlink() else None) != expected:
            raise ValueError("file changed before publication")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Receipt:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "receipt.json"

    @contextmanager
    def lock(self):
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("receipt directory must be private and owned by the operator")
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("invalid receipt lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        info = self.path.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("unsafe receipt permissions")
        data = json.loads(read_file(self.path))
        if (not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") != 1 or
                data.get("owner") != OWNER or data.get("machine") != socket.gethostname()):
            raise ValueError("unsupported or foreign receipt")
        fields = {"schema", "owner", "machine", "account", "home", "source", "comment", "key",
                  "original_line", "installed", "next_installed", "identity", "phase", "marker", "services"}
        if (set(data) != fields or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", data["account"]) or
                not Path(data["home"]).is_absolute() or not Path(data["source"]).is_absolute() or
                not isinstance(data["services"], list) or
                data["phase"] not in {"prepared", "creating-account", "publishing-key",
                                      "removing-source-key", "active", "removing", "removed"}):
            raise ValueError("invalid tunnel receipt")
        identity = data["identity"]
        if identity is not None and (not isinstance(identity, dict) or
                set(identity) != {"name", "uid", "gid", "home", "shell", "comment", "groups", "group_name"} or
                type(identity["uid"]) is not int or type(identity["gid"]) is not int or
                identity["uid"] <= 0 or identity["gid"] <= 0 or identity["name"] != data["account"] or
                identity["home"] != data["home"] or identity["shell"] != "/usr/sbin/nologin" or
                identity["groups"] != [identity["gid"]] or identity["group_name"] != data["account"]):
            raise ValueError("invalid recorded account identity")
        return data

    def save(self, data):
        prior = read_file(self.path) if self.path.exists() else None
        write_file(self.path, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode(), prior)


class Host:
    def __init__(self, admin: Path | None = None) -> None:
        self._admin_option = admin
        self._admin: Path | None = None

    def admin(self) -> str:
        """Resolved and checked when service authority is first needed; setup never needs it."""
        if self._admin is None:
            self._admin = checked_admin(self._admin_option or default_admin())
        return str(self._admin)

    def run(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    def account(self, name):
        try:
            p = pwd.getpwnam(name)
        except KeyError:
            return None
        return {"name": p.pw_name, "uid": p.pw_uid, "gid": p.pw_gid,
                "home": p.pw_dir, "shell": p.pw_shell, "comment": p.pw_gecos,
                "groups": sorted(os.getgrouplist(name, p.pw_gid)), "group_name": grp.getgrgid(p.pw_gid).gr_name}

    def create(self, name, home, marker):
        self.run("useradd", "--system", "--user-group", "--no-create-home",
                 "--home-dir", str(home), "--shell", "/usr/sbin/nologin", "--comment", marker, name)

    def delete(self, identity):
        self.run("userdel", identity["name"])  # no recursive home removal
        self.cleanup_group(identity)

    def cleanup_group(self, identity):
        try:
            group = grp.getgrnam(identity["group_name"])
        except KeyError:
            return
        if group.gr_gid != identity["gid"]:
            raise ValueError("group identity changed; retained")
        if group.gr_mem or any(p.pw_gid == group.gr_gid for p in pwd.getpwall()):
            raise ValueError("recorded group now serves another account; retained")
        self.run("groupdel", group.gr_name)

    def processes(self, uid):
        result = subprocess.run(["pgrep", "-u", str(uid)], capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError("process inspection failed")
        return result.stdout.splitlines()

    def ssh_check(self):
        self.run("sshd", "-t")

    def services(self):
        return json.loads(self.run(self.admin(), "service-list"))

    def guarded_removal_supported(self):
        if "--expected-definition" not in self.run(self.admin(), "service-remove", "--help"):
            raise ValueError("authority needs guarded removal support before service cleanup")

    def remove_service(self, expected):
        self.run(self.admin(), "service-remove", expected["name"], "--expected-definition",
                 json.dumps(expected, sort_keys=True, separators=(",", ":")))


def matched_line(raw, comment):
    lines = raw.decode("utf-8").splitlines(keepends=True)
    matches = [line for line in lines if line.rstrip("\r\n").endswith(" " + comment)]
    if len(matches) != 1:
        raise ValueError("exactly one key with the exact comment is required")
    line = matches[0]
    found = re.search(r"(?:^| )((?:ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+) [A-Za-z0-9+/=]+) " +
                      re.escape(comment) + r"$", line.rstrip("\r\n"))
    if not found:
        raise ValueError("unsupported public key line")
    return line, found.group(1)


def control_plane_target(value):
    """Return the canonical HOST:PORT the tunnel may open; HOST must be an IP literal."""
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("control plane must be IP:PORT")
    address = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    shown = f"[{address}]" if address.version == 6 else str(address)
    return f"{shown}:{int(port)}"


def restricted_key(key, box, comment, control_plane):
    ipaddress.ip_address(box)
    target = control_plane_target(control_plane)
    return ('restrict,port-forwarding,from="' + box +
            '",permitlisten="127.0.0.1:18520",permitlisten="127.0.0.1:18522",' +
            'permitopen="' + target + '" ' + key + " " + comment + "\n")


def verify_account(host, receipt):
    actual = host.account(receipt["account"])
    if actual != receipt["identity"]:
        raise ValueError("account identity or membership changed; retained")
    return actual


def private_directory(path, identity):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != identity["uid"] or info.st_gid != identity["gid"] or
                stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("unowned or unsafe account directory")
    else:
        path.mkdir(mode=0o700)
        os.chown(path, identity["uid"], identity["gid"])


def setup(host, ledger, *, account, home, source, comment, box, control_plane, adopt=False):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", account) or not re.fullmatch(r"[a-zA-Z0-9-]+", comment):
        raise ValueError("invalid account/comment")
    desired_box = str(ipaddress.ip_address(box))
    host.ssh_check()  # before account/key changes
    r = ledger.load()
    keys = home / ".ssh/authorized_keys"
    if r is None:
        actual = host.account(account)
        if actual and not adopt:
            raise ValueError("existing account has no receipt; explicit adoption required")
        if actual:
            if (actual["home"] != str(home) or actual["shell"] != "/usr/sbin/nologin" or
                    actual["groups"] != [actual["gid"]] or actual["group_name"] != account or
                    actual["uid"] <= 0 or actual["gid"] <= 0):
                raise ValueError("existing account does not match the restricted tunnel contract")
            key_raw = read_file(keys)
            line, key = matched_line(key_raw, comment)
            if key_raw.decode() != line:
                raise ValueError("existing account contains additional keys; retained")
            original = None
            installed = line
        else:
            if home.exists() or home.is_symlink():
                raise ValueError("new account home already exists; retained")
            original, key = matched_line(read_file(source), comment)
            installed = None
        r = {"schema": 1, "owner": OWNER, "machine": socket.gethostname(),
             "account": account, "home": str(home), "source": str(source), "comment": comment,
             "key": key, "original_line": original, "installed": installed, "next_installed": None,
             "identity": actual, "phase": "prepared", "marker": OWNER + "-" + secrets.token_hex(12),
             "services": []}
        ledger.save(r)
    if (r["account"], r["home"], r["source"], r["comment"]) != (account, str(home), str(source), comment):
        raise ValueError("receipt belongs to different provisioning inputs")
    if r["phase"] == "removed":
        raise ValueError("receipt is retired; preserve it and use a new receipt directory")
    if r["identity"] is None:
        existing = host.account(account)
        if existing is not None:
            if (existing["comment"] != r["marker"] or existing["home"] != str(home) or
                    existing["shell"] != "/usr/sbin/nologin" or existing["groups"] != [existing["gid"]] or
                    existing["group_name"] != account):
                raise ValueError("unfinished creation has unconfirmed ownership")
        else:
            r["phase"] = "creating-account"; ledger.save(r)
            host.create(account, home, r["marker"])
            existing = host.account(account)
        if (not existing or existing["name"] != account or existing["comment"] != r["marker"] or
                existing["home"] != str(home) or existing["shell"] != "/usr/sbin/nologin" or
                existing["groups"] != [existing["gid"]] or existing["group_name"] != account or
                existing["uid"] <= 0 or existing["gid"] <= 0):
            raise RuntimeError("expected restricted account creation not observed")
        r["identity"] = existing
        ledger.save(r)
    identity = verify_account(host, r)
    private_directory(home, identity)
    private_directory(home / ".ssh", identity)
    # Resume only the recorded publication: either side of that known replacement is acceptable.
    raw = read_file(keys) if keys.exists() or keys.is_symlink() else None
    allowed = [v.encode() if v is not None else None for v in (r["installed"], r["next_installed"])]
    if raw not in allowed:
        raise ValueError("account key file changed; retained")
    desired = restricted_key(r["key"], desired_box, comment, control_plane)
    r["next_installed"] = desired; r["phase"] = "publishing-key"; ledger.save(r)
    if raw is not None:
        info = keys.lstat()
        if info.st_uid != identity["uid"] or info.st_gid != identity["gid"] or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("key ownership/mode changed; retained")
    write_file(keys, desired.encode(), raw, identity)
    r["installed"] = desired; r["next_installed"] = None; r["phase"] = "removing-source-key"; ledger.save(r)
    if r["original_line"] is not None:
        raw = read_file(source)
        original = r["original_line"].encode()
        if raw.splitlines(keepends=True).count(original) == 1:
            write_file(source, raw.replace(original, b"", 1), raw)
        elif any(line.rstrip(b"\r\n").endswith(b" " + comment.encode()) for line in raw.splitlines(keepends=True)):
            raise ValueError("source key changed; retained")
    r["phase"] = "active"; ledger.save(r)
    return {"status": "active", "owned_services": len(r["services"])}


def claim_services(host, ledger, names):
    r = ledger.load()
    if not r or r["phase"] != "active":
        raise ValueError("active tunnel receipt required")
    verify_account(host, r)
    listed = host.services()
    selected = [s for s in listed if s["name"] in names]
    if sorted(s["name"] for s in selected) != sorted(set(names)):
        raise ValueError("every explicitly adopted service must exist exactly once")
    host.guarded_removal_supported()
    r["services"] = selected; ledger.save(r)
    return {"status": "adopted", "services": len(selected)}


def teardown(host, ledger, restore_source=False):
    r = ledger.load()
    if r is None:
        raise ValueError("no ownership receipt; nothing is implicitly removed")
    if r["phase"] == "removed":
        return {"status": "already_removed"}
    if r["identity"] is None:
        raise ValueError("account creation ownership unresolved; resume setup first")
    if r["phase"] == "removing" and host.account(r["account"]) is None:
        if (Path(r["home"]).exists() or Path(r["home"]).is_symlink() or
                any(s["name"] in {v["name"] for v in host.services()} for s in r["services"])):
            raise ValueError("account removed but remaining cleanup is unconfirmed")
        host.cleanup_group(r["identity"])
        r["phase"] = "removed"; ledger.save(r)
        return {"status": "removed", "resumed_after_account_removal": True}
    identity = verify_account(host, r)
    if restore_source and r["original_line"] is None:
        raise ValueError("source grant was not recorded; restoration cannot be inferred")
    if host.processes(identity["uid"]):
        raise ValueError("account still has processes; close its tunnel before teardown")
    home = Path(r["home"]); keys = home / ".ssh/authorized_keys"
    known = {home / ".ssh", keys}
    if home.exists() or home.is_symlink():
        private_directory(home, identity); private_directory(home / ".ssh", identity)
        if any(p not in known for p in home.rglob("*")):
            raise ValueError("home contains unowned files; account and files retained")
    raw = read_file(keys) if keys.exists() or keys.is_symlink() else None
    if raw not in [v.encode() if v is not None else None for v in (r["installed"], r["next_installed"])]:
        raise ValueError("key state changed; retained")
    if r["services"]:
        host.guarded_removal_supported()
        listed = {s["name"]: s for s in host.services()}
        for expected in r["services"]:
            if expected["name"] in listed and listed[expected["name"]] != expected:
                raise ValueError("adopted service definition changed; retained")
    r["phase"] = "removing"; ledger.save(r)
    for expected in r["services"]:
        if expected["name"] in {s["name"] for s in host.services()}:
            host.remove_service(expected) # authority compares in its own transaction
    if restore_source and r["original_line"] is not None:
        source = Path(r["source"]); raw_source = read_file(source)
        original = r["original_line"].encode()
        if original not in raw_source.splitlines(keepends=True):
            if any(line.rstrip(b"\r\n").endswith(b" " + r["comment"].encode()) for line in raw_source.splitlines(keepends=True)):
                raise ValueError("source comment now identifies another key; retained")
            write_file(source, raw_source + (b"" if not raw_source or raw_source.endswith(b"\n") else b"\n") + original, raw_source)
    # Do not delete backup files, wildcard matches, unrelated keys or whole homes.
    if keys.exists():
        keys.unlink()
    if (home / ".ssh").exists():
        (home / ".ssh").rmdir()
    if home.exists():
        home.rmdir()
    host.delete(identity)
    r["phase"] = "removed"; ledger.save(r)
    return {"status": "removed", "source_key_restored": restore_source}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("setup", "claim-services", "teardown"))
    p.add_argument("--state-dir", type=Path, default=Path("/var/lib/boring-pondnt-tunnel"))
    p.add_argument("--account", default="pondnt-tunnel")
    p.add_argument("--home", type=Path, default=Path("/home/pondnt-tunnel"))
    p.add_argument("--source-keys", type=Path,
                   help="authorized_keys file that holds the dev-box key now; required for setup")
    p.add_argument("--comment", help="exact comment of the dev-box key; required for setup")
    p.add_argument("--control-plane", help="IP:PORT of the Crocodile control plane the box may reach; required for setup")
    p.add_argument("--box", help="current dev-box source IP; required for setup")
    p.add_argument("--adopt-account", action="store_true")
    p.add_argument("--services", nargs="+")
    p.add_argument("--restore-source", action="store_true")
    p.add_argument("--admin", type=Path,
                   help="boring-crocodile-admin to use for claim-services and teardown (default: "
                        f"<HOME>/current/bin/boring-crocodile-admin, HOME from {HOMES} or {CROCODILE_HOME})")
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("host provisioning requires the operator's root session")
    if a.action == "setup":
        missing = [flag for flag, value in (("--box", a.box), ("--source-keys", a.source_keys),
                                            ("--comment", a.comment), ("--control-plane", a.control_plane))
                   if not value]
        if missing:
            p.error("setup needs explicit " + ", ".join(missing))
        try:
            control_plane_target(a.control_plane)
        except ValueError as error:
            p.error(str(error))
    if not a.state_dir.is_absolute() or not a.home.is_absolute() or any(
            path is not None and not path.is_absolute() for path in (a.source_keys, a.admin)):
        p.error("all configured paths must be absolute")
    ledger = Receipt(a.state_dir)
    with ledger.lock():
        host = Host(a.admin)
        if a.action == "setup":
            answer = setup(host, ledger, account=a.account, home=a.home, source=a.source_keys,
                           comment=a.comment, box=a.box, control_plane=a.control_plane,
                           adopt=a.adopt_account)
        elif a.action == "claim-services":
            if not a.services:
                p.error("claim-services needs explicit --services names")
            answer = claim_services(host, ledger, a.services)
        else:
            answer = teardown(host, ledger, a.restore_source)
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
