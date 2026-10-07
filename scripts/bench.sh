#!/usr/bin/env bash
# macOS entry point: locate an interpreter that has fpsbench installed and hand
# the arguments over unchanged.
#
# Usage: scripts/bench.sh doctor | record ... | bench ... | pick-region | dry-run
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

if [[ -x "$root/.venv/bin/python" ]]; then
  python_bin="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
else
  echo "no python3 found: install Python 3.10+ (or 3.9) first" >&2
  exit 1
fi

exec "$python_bin" -m fpsbench "$@"
