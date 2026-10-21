"""Stage installer JSON before publication; ordinary edits use in-place writes."""
import argparse
import json
import sys
from .textfiles import _write_text
from . import disk
from boring_service_kit.outcomes import Outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        _write_text(args.path, json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                   args.expected_sha, encoding="utf-8", bom=False, newline="crlf",
                    publisher=disk.replace_atomically)
    except Outcome as error:
        print("configuration publication refused or failed: " + error.code, file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("configuration publication failed; inspect current file before retrying", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
