#!/usr/bin/env python3
"""The one check for boring-pondnt: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist \\
        --services-root /path/to/boring-services [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-pondnt from a copy of its tracked files, and runs the
tests there. On Linux the tests marked `windows` skip themselves; the Windows
run is the release zip's run-tests.cmd. Needs uv (PATH or $UV) and the package index (or uv's cache)
for the locked wheels and pytest. Exit status 0 means every step passed.
"""
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]


SERVICE_PACKAGES = ("sourcecache-d365", "knowledge", "canvas", "mcp-ferry")


def main():
    parser = develop.arguments(__doc__)
    parser.add_argument("--services-root", type=Path, required=True)
    args = parser.parse_args()
    services = args.services_root.resolve()
    with tempfile.TemporaryDirectory(prefix="pondnt-check-") as tmp:
        tmp = Path(tmp)
        packages = [develop.package(ROOT, tmp / "app")] + [develop.package(services, tmp / s, s) for s in SERVICE_PACKAGES]
        python = develop.environment(develop.uv(), args.python, tmp / "venv",
                                     [ROOT / "requirements.lock", ROOT / "requirements-tests.lock"],
                                     args.common_wheels, packages, services / "scripts/build-constraints.txt")
        develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", cwd=ROOT)
    print("all boring-pondnt checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
