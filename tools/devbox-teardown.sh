#!/usr/bin/env bash
# Optional host helper; private receipt owns all effects. See DEVBOX_TUNNEL.md.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/tunnel_host.py" teardown "$@"
