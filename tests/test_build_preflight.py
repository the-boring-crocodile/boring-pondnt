"""Release inputs are checked before output, network or build tools are touched."""
import importlib.util
from pathlib import Path
import subprocess
import sys

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
    monkeypatch.setattr(module, "run", forbidden)
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
