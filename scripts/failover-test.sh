#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
if [[ "${1:-}" == "--azure" ]]; then
  shift
  exec bash scripts/azure-failover.sh "$@"
fi
exec "${PYTHON:-.venv/bin/python}" scripts/failover-test.py \
  --base-url "${DEMO_BASE_URL:-http://127.0.0.1:8080}" "$@"
