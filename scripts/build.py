#!/usr/bin/env python3
"""Build the Windows release zip on Linux.

    scripts/build.py --services-root /path/boring-services --common-wheels /path/common/dist
        [--with-tests --common-source /path/boring-pond-common] [--out dist]

Layout of `boring-pondnt-<version>-win-x64/`:

    python/            embeddable CPython, our package and its Windows wheels
                       in python/Lib/site-packages
    pwsh/              portable PowerShell 7
    INSTALL.md         how to install, check and remove (docs/INSTALL.md)
    LICENSE, COPYING.md  our licence (AGPL-3.0-or-later; the output of the tools is yours)
    setup.ps1          looks, shows the plan, sets up .mcp.json and the index on "yes" (tools/setup.ps1)
    devbox-defender.ps1  Defender exclusions for the D365 box: report, -Apply, -Undo (tools/devbox-defender.ps1)
    extras/pond-tunnel/  optional: the Pond tunnel we test with (docs/DEVBOX_TUNNEL.md, tools/)
    handbook/          the working agreement (`bootstrap` serves it)
    boring-pondnt-core.cmd    launcher: boring-pondnt-core.cmd [--self-check] ...
    boring-sourcecache-d365.cmd, boring-knowledge.cmd, boring-canvas-server.cmd  the bundled services
    boring-pondnt-d365.cmd    launcher: boring-pondnt-d365.cmd serve ... (D365 operations plus read-only xref)
    MANIFEST.json      versions, input hashes, git commit
    tests/, pytest.ini, run-tests.cmd   with --with-tests (for CI)

Dependencies come from the committed hash locks (requirements.lock, and
requirements-tests.lock with --with-tests; scripts/lock.py writes them) and are
installed with `uv --python-platform x86_64-pc-windows-msvc`, because pip
evaluates `sys_platform == "win32"` markers against the build host and silently
drops Windows-only dependencies such as pywin32.
Downloads are cached in ~/lab/boring-pondnt-cache and their sha256 recorded.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import json
import shutil
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _develop import load  # noqa: E402

# boring_deploy.develop, the steps every component shares; loaded from the Common
# wheels given to build() (after their check), so importing this script has no effect.
develop: Any = None

PYTHON_VERSION = "3.14.8"
PWSH_VERSION = "7.6.6"
MCP_VERSION = "2.2.0"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PWSH_URL = (f"https://github.com/PowerShell/PowerShell/releases/download/v{PWSH_VERSION}/"
            f"PowerShell-{PWSH_VERSION}-win-x64.zip")
# Expected SHA-256 of the two downloads, from their publishers: python.org's
# SBOM (python-<v>-embed-amd64.zip.spdx.json) and the GitHub release asset
# digest. Update together with the versions; a mismatch stops the build.
PYTHON_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
PWSH_SHA256 = "02fe458be20493fbdf43f61ea20610b811ee6c738ab1676c61b9cfcd1a33c860"
TARGET = ["--python-platform", "x86_64-pc-windows-msvc", "--python-version", PYTHON_VERSION.rsplit(".", 1)[0],
          "--only-binary", ":all:"]

REPO = Path(__file__).resolve().parent.parent
CACHE = Path.home() / "lab" / "boring-pondnt-cache"

PYTEST_INI = """[pytest]
testpaths = tests
pythonpath = tests
markers =
    windows: needs real Windows (process trees, console encoding, locks)
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, expected_sha256: str) -> Path:
    """Download once into the cache; every use (cache hit or not) is hash-checked."""
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / url.rsplit("/", 1)[1]
    if not target.exists():
        print(f"downloading {url}", flush=True)
        partial = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        if sha256(partial) != expected_sha256:
            partial.unlink()
            raise SystemExit(f"{url}: SHA-256 differs from the pinned value; download discarded")
        partial.rename(target)
    if sha256(target) != expected_sha256:
        raise SystemExit(f"{target}: SHA-256 differs from the pinned value; remove it and check the source")
    return target


def package_version() -> str:
    text = (REPO / "src" / "boring_pondnt" / "__init__.py").read_text()
    return text.split('__version__ = "', 1)[1].split('"', 1)[0]


def write_services_test_config(stage: Path) -> None:
    # Embedded Python ignores PYTHONPATH; packaged sibling fixtures belong on pytest's path.
    (stage / "services-tests/pytest.ini").write_text(
        "[pytest]\ntestpaths = .\npythonpath = . kit/tests sourcecache-d365/tests canvas/tests mcp-ferry/tests\n"
        "addopts = --import-mode=importlib\n")


def copy_service_integration_tests(services_root: Path, target: Path) -> None:
    """Ship runtime integration tests; source-build/operator tests stay with source."""
    develop.plain_copy(services_root / "tests", target)
    (target / "test_build_preflight.py").unlink(missing_ok=True)  # tests the source build
    # The Linux Docker installer's tests test boring-deploy on Linux, not the services this
    # bundle runs: every test that imports boring_deploy stays with the source (by its
    # imports, not a list of names, which once missed one).
    for path in target.glob("test_*.py"):
        if re.search(r"^\s*(import|from)\s+boring_deploy\b", path.read_text(encoding="utf-8"), re.M):
            path.unlink()
    (target / "test_default_home.py").unlink(missing_ok=True)  # reads the source tree's README


def enable_site_packages(python_dir: Path) -> None:
    """The embeddable build ignores site-packages until its ._pth file allows it."""
    (pth,) = python_dir.glob("python3*._pth")
    lines = [line for line in pth.read_text().splitlines() if line.strip() not in ("#import site", "import site")]
    lines += ["Lib\\site-packages", "import site"]
    pth.write_text("\r\n".join(lines) + "\r\n")


COMMON = ("boring_registry", "boring_service_kit", "boring_file_core")
LOCAL_PACKAGES = ("boring-pondnt", "boring-sourcecache-d365", "boring-knowledge", "boring-canvas", "boring-mcp-ferry")


def common_preflight(common_wheels: Path, common_source: Path | None, with_tests: bool) -> dict:
    """Pond Common comes from its release wheels; every input is checked before any effect."""
    if not common_wheels.is_dir():
        raise FileNotFoundError(f"Common wheels directory missing: {common_wheels}")
    wheels = {}
    for name in COMMON:
        found = sorted(common_wheels.glob(f"{name}-*.whl"))
        if len(found) != 1:
            raise FileNotFoundError(f"expected exactly one {name} wheel in {common_wheels}, found {len(found)}")
        wheels[found[0].name] = sha256(found[0])
    if with_tests:
        kit_tests = (common_source / "packages/boring-service-kit/tests") if common_source else None
        if kit_tests is None or not kit_tests.is_dir():
            raise FileNotFoundError("--with-tests needs --common-source (a boring-pond-common checkout) for the kit tests")
    return {"common_wheels": wheels, "common_source": str(common_source) if common_source else None}


def local_pins(wheels: list[Path]) -> str:
    """Hash pins for the packages built here, each at its own wheel's version."""
    text = ""
    for built in sorted(wheels):
        wheel_name, wheel_version = built.name.split("-")[:2]
        if wheel_name.replace("_", "-") in LOCAL_PACKAGES:
            text += f"{wheel_name.replace('_', '-')}=={wheel_version} --hash=sha256:{sha256(built)}\n"
    return text


def check_release_version(app_version: str, release_name: str, wheels: list[Path], installed: list[str]) -> None:
    """The release names one pondNT version: its name, the built wheel and the installed metadata agree.

    A loop variable once overwrote the version and the manifest named another
    package's version as pondNT's.
    """
    found = {
        "release name": [release_name.split("-")[2]],
        "wheel": [w.name.split("-")[1] for w in wheels if w.name.startswith("boring_pondnt-")],
        "installed metadata": [d[: -len(".dist-info")].split("-")[1] for d in installed
                               if d.startswith("boring_pondnt-") and d.endswith(".dist-info")],
    }
    wrong = {where: versions for where, versions in found.items() if versions != [app_version]}
    if wrong:
        raise SystemExit(f"pondNT version {app_version} disagrees with {json.dumps(wrong)}")


def build(with_tests: bool, out: Path, services_root: Path, common_wheels: Path,
          common_source: Path | None = None) -> Path:
    global develop
    common = common_preflight(common_wheels, common_source, with_tests)
    _, develop = load(common_wheels)
    app_version = package_version()  # pondNT's; each wheel keeps its own (local_pins)
    commit = develop.git(REPO, "rev-parse", "--short", "HEAD").strip()
    dirty = bool(develop.git(REPO, "status", "--porcelain").strip())
    release_name = f"boring-pondnt-{app_version}-win-x64" + ("-tests" if with_tests else "")
    stage = out / release_name
    if stage.exists() or (out / f"{release_name}.zip").exists():
        raise SystemExit("release already exists; choose a new output directory/version")
    stage.mkdir(parents=True)

    python_zip, pwsh_zip = fetch(PYTHON_URL, PYTHON_SHA256), fetch(PWSH_URL, PWSH_SHA256)
    with zipfile.ZipFile(python_zip) as archive:
        archive.extractall(stage / "python")
    enable_site_packages(stage / "python")
    with zipfile.ZipFile(pwsh_zip) as archive:
        archive.extractall(stage / "pwsh")

    wheel_dir = stage / "wheels"  # owned by this new release; never sweep a shared output path
    # Each wheel is built from a copy of its tracked package files (develop.package), as the
    # check and the lock do: nothing is built inside the trees.
    # uv with --only-binary installs wheels only, never builds from source.
    with tempfile.TemporaryDirectory(prefix="boring-pondnt-build-") as scratch:
        for name in common["common_wheels"]:
            wheel_dir.mkdir(exist_ok=True)
            shutil.copyfile(common_wheels / name, wheel_dir / name)
        for package, root, sub in (("boring-pondnt", REPO, "."),
                                   ("boring-sourcecache-d365", services_root, "sourcecache-d365"),
                                   ("boring-knowledge", services_root, "knowledge"),
                                   ("boring-canvas", services_root, "canvas"),
                                   ("boring-mcp-ferry", services_root, "mcp-ferry")):  # the optional gateway (D41)
            source = develop.package(root, Path(scratch) / package, sub)
            develop.run(develop.uv(), "build", "--build-constraint", services_root / "scripts/build-constraints.txt",
                        "--wheel", "--out-dir", wheel_dir, source)
    (wheel,) = wheel_dir.glob("boring_pondnt-*.whl")
    (registry,) = wheel_dir.glob("boring_registry-*.whl")
    (kit,) = wheel_dir.glob("boring_service_kit-*.whl")
    # The committed locks (scripts/lock.py) pin everything but the five packages
    # built above; those join with the hashes of the wheels just built. uv
    # evaluates the universal lock's markers for the Windows target.
    lock = stage / "requirements.txt"
    text = (REPO / "requirements.lock").read_text()
    if with_tests:
        text += (REPO / "requirements-tests.lock").read_text()
    text += local_pins(list(wheel_dir.glob("*.whl")))
    lock.write_text(text)
    develop.run(develop.uv(), "pip", "install", "--quiet", "--target", stage / "python" / "Lib" / "site-packages",
                *TARGET, "--require-hashes", "--no-deps", "--find-links", wheel_dir, "-r", lock)
    # pywin32 brings two programs pondNT never runs: Pythonwin (an MFC editor)
    # and the Windows-service host. The service kit uses win32api and win32job
    # only. Unused executables in a downloaded zip only draw antivirus attention.
    site_packages = stage / "python" / "Lib" / "site-packages"
    shutil.rmtree(site_packages / "pythonwin")
    (site_packages / "win32" / "pythonservice.exe").unlink()

    develop.plain_copy(REPO / "handbook", stage / "handbook")
    develop.plain_copy(REPO / "docs", stage / "docs")  # installed guides referenced by the handbook
    # Optional, not the default: the Pond tunnel we test with, shipped, not installed.
    extras = stage / "extras" / "pond-tunnel"
    extras.mkdir(parents=True)
    shutil.copyfile(REPO / "docs" / "DEVBOX_TUNNEL.md", extras / "README.md")
    for tool in ("devbox-start.ps1", "maintenance-state.ps1", "tunnel-account.sh",
                 "devbox-teardown.sh", "tunnel_host.py"):
        shutil.copyfile(REPO / "tools" / tool, extras / tool)
    for script in ("setup.ps1", "devbox-defender.ps1", "maintenance-state.ps1"):
        (stage / script).write_bytes((REPO / "tools" / script).read_bytes().replace(b"\r\n", b"\n")
                                     .replace(b"\n", b"\r\n"))
    (stage / "INSTALL.md").write_bytes((REPO / "docs" / "INSTALL.md").read_bytes().replace(b"\r\n", b"\n")
                                       .replace(b"\n", b"\r\n"))  # Notepad-friendly
    develop.plain_copy(services_root / "examples", stage / "examples")
    for name in ("LICENSE", "COPYING.md"):  # the bundle carries our licence
        shutil.copyfile(REPO / name, stage / name)
    shutil.copyfile(services_root / "docs/CONFIGURATION.md", stage / "SERVICES_CONFIGURATION.md")
    # PYTHONUTF8=1: Python's own stdio in UTF-8 even when redirected (Windows would use the
    # ANSI code page, and the self-check would crash on that).
    (stage / "boring-pondnt-core.cmd").write_text(
        '@echo off\r\nset PYTHONUTF8=1\r\n"%~dp0python\\python.exe" -m boring_pondnt.server %*\r\n')
    (stage / "boring-pondnt-d365.cmd").write_text(
        '@echo off\r\nset PYTHONUTF8=1\r\n"%~dp0python\\python.exe" -m boring_pondnt.d365_server %*\r\n')
    for name, module in (("boring-sourcecache-d365", "boring_sourcecache_d365.server"),
                         ("boring-knowledge", "boring_knowledge.server"),
                         ("boring-canvas-server", "boring_canvas.server"),
                         ("boring-mcp-ferry", "boring_mcp_ferry.server"),
                         ("boring-service-keys", "boring_service_kit.keys")):
        (stage / f"{name}.cmd").write_text(
            '@echo off\r\nset PYTHONUTF8=1\r\n"%~dp0python\\python.exe" -m ' + module + ' %*\r\n')
    if with_tests:
        develop.plain_copy(REPO / "tests", stage / "tests")
        # Builder parity is a source-tree gate, already run on Linux. The
        # runtime bundle deliberately has neither builder nor package sources.
        for source_only in ("test_build_preflight.py", "test_tunnel_host.py"):
            (stage / "tests" / source_only).unlink()
        for name in ("knowledge", "canvas", "sourcecache-d365", "mcp-ferry"):
            fixture = stage / "services-tests" / name
            develop.plain_copy(services_root / name / "tests", fixture / "tests")
            shutil.copyfile(services_root / name / "pyproject.toml", fixture / "pyproject.toml")
        for drawing in ("rose.svg", "ausmalbilder.svg"):
            shutil.copyfile(services_root / "canvas" / drawing, stage / "services-tests/canvas" / drawing)
        develop.plain_copy(common_source / "packages/boring-service-kit", stage / "services-tests" / "kit")
        develop.plain_copy(services_root / "examples", stage / "services-tests" / "examples")  # knowledge's examples test
        copy_service_integration_tests(services_root, stage / "services-tests" / "integration")
        write_services_test_config(stage)
        (stage / "run-services-tests.cmd").write_text(
            '@echo off\r\ncd /d "%~dp0"\r\nset PYTHONUTF8=1\r\n'
            '"%~dp0python\\python.exe" -m pytest -q -c services-tests/pytest.ini services-tests %*\r\n')
        (stage / "pytest.ini").write_text(PYTEST_INI.replace("\n", "\r\n"))
        (stage / "run-tests.cmd").write_text(
            '@echo off\r\ncd /d "%~dp0"\r\nset PYTHONUTF8=1\r\n"%~dp0python\\python.exe" -m pytest -q -rs %*\r\n')

    installed = sorted(p.name for p in (stage / "python" / "Lib" / "site-packages").glob("*.dist-info"))
    check_release_version(app_version, release_name, list(wheel_dir.glob("*.whl")), installed)
    manifest = {
        "name": release_name,
        "common": common,
        "boring-pondnt": app_version,
        "commit": commit + ("-dirty" if dirty else ""),
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": {"version": PYTHON_VERSION, "url": PYTHON_URL, "sha256": sha256(python_zip)},
        "pwsh": {"version": PWSH_VERSION, "url": PWSH_URL, "sha256": sha256(pwsh_zip)},
        "packages": [d[: -len(".dist-info")] for d in installed],
        "with_tests": with_tests,
        "services_commit": develop.git(services_root, "rev-parse", "HEAD").strip(),
        "services_dirty": bool(develop.git(services_root, "status", "--porcelain").strip()),
        "uv": develop.run(develop.uv(), "--version", capture_output=True, text=True).stdout.strip(),
        "build_constraints_sha256": sha256(services_root / "scripts/build-constraints.txt"),
        "requirements_sha256": sha256(lock),
        "wheel_sha256": {p.name: sha256(p) for p in sorted(wheel_dir.glob("*.whl"))},
    }
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")

    archive_path = out / f"{release_name}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, f"{release_name}/{path.relative_to(stage).as_posix()}")
    shutil.copyfile(stage / "INSTALL.md", out / "INSTALL.md")  # readable before unpacking
    print(json.dumps({"zip": str(archive_path), "bytes": archive_path.stat().st_size,
                      "commit": manifest["commit"]}))
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with-tests", action="store_true", help="include tests, pytest and run-tests.cmd")
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    parser.add_argument("--services-root", type=Path, required=True, help="explicit boring-services checkout to package")
    parser.add_argument("--common-wheels", type=Path, required=True, help="Pond Common release wheels (registry, service-kit, file-core)")
    parser.add_argument("--common-source", type=Path, help="boring-pond-common checkout; required with --with-tests (kit tests)")
    args = parser.parse_args()
    build(args.with_tests, args.out.resolve(), args.services_root.resolve(), args.common_wheels.resolve(),
          args.common_source.resolve() if args.common_source else None)


if __name__ == "__main__":
    main()
