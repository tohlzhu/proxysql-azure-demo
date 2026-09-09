#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -f .env ]]; then
  exec "${PYTHON:-.venv/bin/python}" scripts/with-env.py .env \
    "${PYTHON:-.venv/bin/python}" scripts/run-tests.py "$@"
fi
exec "${PYTHON:-.venv/bin/python}" scripts/run-tests.py "$@"
