#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p artifacts
if [[ "${1:-}" == "--azure" ]]; then
  shift
  "${PYTHON:-.venv/bin/python}" scripts/collect-azure-results.py "$@"
else
  [[ $# -eq 0 ]] || { echo "Usage: collect-results.sh [--azure [--base-url URL]]" >&2; exit 2; }
  scripts/configure-proxysql.sh --stats-only > artifacts/proxysql-stats.jsonl
  curl --fail --silent --show-error "${DEMO_BASE_URL:-http://127.0.0.1:8080}/demo/config" \
    --output artifacts/health.json
  curl --fail --silent --show-error "${DEMO_BASE_URL:-http://127.0.0.1:8080}/metrics" \
    --output artifacts/metrics.prom
fi
"${PYTHON:-.venv/bin/python}" scripts/collect-results.py
