"""Release inputs are checked before output, network or build tools are touched."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pondnt_builder", ROOT / "scripts/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    common = tmp_path / "common-dist"
    common.mkdir()
    for name in module.COMMON:
        (common / f"{name}-0.4.0-py3-none-any.whl").write_bytes(b"wheel")
    services = tmp_path / "services"
    services.mkdir()
    def forbidden(*args, **kwargs):
        pytest.fail("build reached effects before its input checks")

    def plain_copy(source, target):
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        return target
    # boring_deploy.develop comes from real Common wheels; these fake ones stand in for it here
    develop = SimpleNamespace(plain_copy=plain_copy, run=forbidden, git=forbidden, uv=forbidden,
                              package=forbidden)
    monkeypatch.setattr(module, "develop", develop)
    monkeypatch.setattr(module, "load", forbidden)
    monkeypatch.setattr(module, "fetch", forbidden)
    return module, services, common


def test_complete_common_wheels_pass(builder):
    module, _, common = builder
    assert len(module.common_preflight(common, None, False)["common_wheels"]) == 3


@pytest.mark.parametrize("problem", ["missing-dir", "missing-wheel", "two-versions", "tests-without-source"])
def test_bad_common_input_refused_before_effects(builder, tmp_path, problem):
    module, services, common = builder
    with_tests = problem == "tests-without-source"
    if problem == "missing-dir":
        common = tmp_path / "absent"
    elif problem == "missing-wheel":
        (common / "boring_file_core-0.4.0-py3-none-any.whl").unlink()
    elif problem == "two-versions":
        (common / "boring_registry-0.4.1-py3-none-any.whl").write_bytes(b"wheel")
    out = tmp_path / "out"
    out.mkdir()
    sentinel = out / "wheel" / "operator-file"
    sentinel.parent.mkdir()
    sentinel.write_text("keep")
    with pytest.raises(FileNotFoundError):
        module.build(with_tests, out, services, common)
    assert sorted(p.name for p in out.iterdir()) == ["wheel"]
    assert sentinel.read_text() == "keep"


def test_packaged_integration_collects_without_source_checkout(builder, tmp_path):
    module, services, _ = builder
    source = services / "tests"
    source.mkdir()
    (source / "test_build_preflight.py").write_text("raise RuntimeError(\"source-only dependency absent\")\n")
    (source / "test_authenticated_services.py").write_text("def test_runtime_contract(): pass\n")
    target = tmp_path / "isolated-bundle" / "integration"
    module.copy_service_integration_tests(services, target)
    result = subprocess.run([sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", str(target)],
                            cwd=target.parent, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_runtime_contract" in result.stdout
    assert len(list(source.glob("test_*.py"))) == 2  # packaging never edits its source




def test_packaged_ferry_tests_can_import_their_sibling_fixture(builder, tmp_path):
    module, _, _ = builder
    stage = tmp_path / "isolated-bundle"
    tests = stage / "services-tests" / "mcp-ferry" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_ferry.py").write_text("def backend_fixture(): return 'carried'\n")
    (tests / "test_gateway_arguments.py").write_text(
        "import test_ferry as fixture\n"
        "def test_gateway_admission_fixture(): assert fixture.backend_fixture() == 'carried'\n")
    module.write_services_test_config(stage)
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-q", "-c", str(stage / "services-tests/pytest.ini")],
        cwd=stage, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_each_wheel_keeps_its_version_and_the_release_its_own(builder, tmp_path):
    """The lock loop once overwrote pondNT's version with the last wheel's."""
    module, _, _ = builder
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name in ("boring_pondnt-0.2.0rc16.dev3-py3-none-any.whl",
                 "boring_sourcecache_d365-0.1.10.dev7-py3-none-any.whl",
                 "boring_registry-0.7.0-py3-none-any.whl"):
        (wheels / name).write_bytes(name.encode())
    built = sorted(wheels.glob("*.whl"))
    pins = module.local_pins(built)
    assert "boring-pondnt==0.2.0rc16.dev3 --hash" in pins
    assert "boring-sourcecache-d365==0.1.10.dev7 --hash" in pins
    assert "boring-registry" not in pins  # pinned by the committed lock, not here
    installed = ["boring_pondnt-0.2.0rc16.dev3.dist-info", "boring_sourcecache_d365-0.1.10.dev7.dist-info"]
    name = "boring-pondnt-0.2.0rc16.dev3-win-x64-tests"
    module.check_release_version("0.2.0rc16.dev3", name, built, installed)
    with pytest.raises(SystemExit, match="release name"):
        module.check_release_version("0.2.0rc16.dev3", "boring-pondnt-0.1.10.dev7-win-x64", built, installed)
    with pytest.raises(SystemExit, match="installed metadata"):
        module.check_release_version("0.2.0rc16.dev3", name, built, installed[1:])
    with pytest.raises(SystemExit, match="wheel"):
        module.check_release_version("0.1.10.dev7", "boring-pondnt-0.1.10.dev7-win-x64", built,
                                     ["boring_pondnt-0.1.10.dev7.dist-info"])


@pytest.mark.parametrize("version,folder", [
    ("0.2.10rc1", "pondnt-0.2.10-rc1"),
    ("0.2.10rc6", "pondnt-0.2.10-rc6"),
    ("0.2.10rc12", "pondnt-0.2.10-rc12"),
    ("0.2.10", "boring-pondnt-0.2.10-win-x64"),
])
@pytest.mark.parametrize("with_tests", [False, True])
def test_release_archive_preserves_candidate_version_and_install_folder(builder, tmp_path, version, folder, with_tests):
    import json
    import zipfile
    module, _, _ = builder
    name, install = module.release_layout(version, with_tests)
    suffix = "-tests" if with_tests else ""
    assert name == f"boring-pondnt-{version}-win-x64{suffix}"
    assert install == folder + suffix
    stage = tmp_path / install
    stage.mkdir()
    manifest = {"name": name, "boring-pondnt": version, "install_folder": install}
    (stage / "MANIFEST.json").write_text(json.dumps(manifest))
    archive = module.write_release_archive(stage, tmp_path, name)
    assert archive.name == name + ".zip"
    with zipfile.ZipFile(archive) as released:
        assert released.namelist() == [install + "/MANIFEST.json"]
        assert json.loads(released.read(install + "/MANIFEST.json")) == manifest
    wheel = stage / f"boring_pondnt-{version}-py3-none-any.whl"
    module.check_release_version(version, name, [wheel], [f"boring_pondnt-{version}.dist-info"])


def test_release_archive_depends_on_names_and_contents_only(builder, tmp_path):
    """Two builds of one commit: the same files, other mtimes and modes, the same zip."""
    import os
    import zipfile
    module, _, _ = builder
    archives = []
    for run, (mtime, mode) in enumerate(((1_700_000_000, 0o644), (1_800_000_000, 0o600))):
        stage = tmp_path / f"run{run}" / "pondnt-0.2.10"
        (stage / "python").mkdir(parents=True)
        for name, text in (("MANIFEST.json", "{}"), ("python/a.py", "print(1)")):
            (stage / name).write_text(text)
            (stage / name).chmod(mode)
            os.utime(stage / name, (mtime, mtime))
        archives.append(module.write_release_archive(stage, stage.parent, "release", (2026, 11, 7, 12, 0, 0)))
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with zipfile.ZipFile(archives[0]) as released:
        assert {entry.date_time for entry in released.infolist()} == {(2026, 11, 7, 12, 0, 0)}


def test_uv_cache_info_goes_with_its_record_line(builder, tmp_path):
    """uv's uv_cache.json carries the install's time; RECORD keeps every other line."""
    module, _, _ = builder
    info = tmp_path / "site-packages" / "boring_registry-0.7.12.dist-info"
    info.mkdir(parents=True)
    (info / "uv_cache.json").write_text('{"timestamp":{"secs_since_epoch":1}}')
    (info / "RECORD").write_text("boring_registry/__init__.py,sha256=abc,10\n"
                                 "boring_registry-0.7.12.dist-info/uv_cache.json,sha256=def,40\n"
                                 "boring_registry-0.7.12.dist-info/RECORD,,\n")
    other = tmp_path / "site-packages" / "anyio-4.15.1.dist-info"
    other.mkdir()
    (other / "RECORD").write_text("anyio/__init__.py,sha256=x,1\n")
    assert module.drop_uv_cache_info(tmp_path / "site-packages") == ["boring_registry-0.7.12.dist-info"]
    assert not (info / "uv_cache.json").exists()
    assert (info / "RECORD").read_text() == ("boring_registry/__init__.py,sha256=abc,10\n"
                                             "boring_registry-0.7.12.dist-info/RECORD,,\n")
    assert (other / "RECORD").read_text() == "anyio/__init__.py,sha256=x,1\n"


def test_source_wheel_and_server_agree_on_the_release_version(builder):
    import re
    from importlib.metadata import version
    from boring_pondnt import __version__
    from boring_pondnt.server import bootstrap_result
    module, _, _ = builder
    source = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)[1]
    assert source == module.package_version() == __version__ == version("boring-pondnt")
    assert bootstrap_result(None)["version"] == __version__


def test_the_manifest_names_the_checked_app_version():
    """The manifest takes pondNT's version from the variable the check guards, nothing reassigns it."""
    import ast
    tree = ast.parse((ROOT / "scripts/build.py").read_text())
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build")
    assigned = [t.id for n in ast.walk(build) if isinstance(n, (ast.Assign, ast.For))
                for t in ast.walk(n.targets[0] if isinstance(n, ast.Assign) else n.target)
                if isinstance(t, ast.Name)]
    assert assigned.count("app_version") == 1
    manifest = next(n.value for n in ast.walk(build) if isinstance(n, ast.Assign)
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "manifest")
    entry = dict(zip([k.value for k in manifest.keys], manifest.values))["boring-pondnt"]
    assert isinstance(entry, ast.Name) and entry.id == "app_version"
