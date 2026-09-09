#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Only remove this Compose project's containers. Data is retained unless explicitly requested.
if [[ "${1:-}" == "--volumes" ]]; then
  exec scripts/docker.sh compose down --volumes
elif [[ $# -eq 0 ]]; then
  exec scripts/docker.sh compose down
else
  echo "Usage: local-down.sh [--volumes]" >&2
  exit 2
fi
