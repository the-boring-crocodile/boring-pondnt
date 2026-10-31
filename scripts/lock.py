#!/usr/bin/env python3
"""Regenerate requirements.lock and requirements-tests.lock, pinned with hashes.

One universal lock (Python 3.10-3.14, all platforms) for everything the release
ships besides the five packages built from source at release time (boring-pondnt
and, from boring-services, sourcecache-d365, knowledge, canvas and mcp-ferry). Pond Common
comes from its released wheels (--common-wheels, until Common is on a package
index); everything else from the package index. Existing pins and hashes are
kept unless you pass --upgrade-package NAME or --fresh.

    python scripts/lock.py --common-wheels /path/to/boring-pond-common/dist \\
        --services-root /path/to/boring-services
"""
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]
LOCAL = (("boring-pondnt", None), ("boring-sourcecache-d365", "sourcecache-d365"),
         ("boring-knowledge", "knowledge"), ("boring-canvas", "canvas"), ("boring-mcp-ferry", "mcp-ferry"))
TESTS = "pytest==9.1.1\n"
COMMAND = "python scripts/lock.py --common-wheels <Common dist> --services-root <boring-services>"


def main():
    parser = develop.lock_arguments(__doc__)
    parser.add_argument("--services-root", type=Path, required=True)
    args = parser.parse_args()
    services = args.services_root.resolve()
    with tempfile.TemporaryDirectory(prefix="pondnt-lock-") as tmp:
        tmp = Path(tmp)
        wheels = tmp / "wheels"
        for name, sub in LOCAL:
            stage = develop.package(ROOT if sub is None else services, tmp / name, sub or ".")
            develop.run(develop.uv(), "build", "--quiet", "--wheel", "--build-constraint",
                        services / "scripts/build-constraints.txt", "--out-dir", wheels, stage)
        (tmp / "requirements.in").write_text("".join(name + "\n" for name, _ in LOCAL))
        (tmp / "tests.in").write_text(TESTS)
        develop.lock(args, ROOT / "requirements.lock", [tmp / "requirements.in"], find_links=[wheels],
                     no_emit=[name for name, _ in LOCAL], how=COMMAND)
        # Test tools only, constrained by the runtime lock so both install together.
        develop.lock(args, ROOT / "requirements-tests.lock", [tmp / "tests.in"], find_links=[wheels],
                     extra=["--constraint", str(ROOT / "requirements.lock")], how=COMMAND)
    return 0


if __name__ == "__main__":
    sys.exit(main())
