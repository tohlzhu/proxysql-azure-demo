#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
if [[ "${1:-}" == "--host" ]]; then
  exec "${PYTHON:-python3}" scripts/proxysql-admin.py "$@"
fi
for node in proxysql-1 proxysql-2; do
  container="$(scripts/docker.sh compose ps -q "$node")"
  [[ -n "$container" ]] || { echo "$node is not running" >&2; exit 1; }
  scripts/docker.sh run --rm --network "container:$container" --env-file .env \
    proxysql-demo-app:local python /app/scripts/proxysql-admin.py "$@"
done
