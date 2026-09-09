#!/usr/bin/env bash
set -euo pipefail
AZURE_DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AZURE_DEMO_ROOT
azure_demo() {
  cd "$AZURE_DEMO_ROOT"
  exec "${PYTHON:-python3}" infra/azure_ops.py "$@"
}
