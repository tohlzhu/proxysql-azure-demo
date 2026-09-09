#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p artifacts
"${PYTHON:-.venv/bin/python}" -m ratelimit_demo.cli \
  --base-url "${DEMO_BASE_URL:-http://127.0.0.1:8080}" --scenario distributed \
  --duration "${DEMO_DURATION:-6}" --mode distributed --output artifacts/distributed.json
