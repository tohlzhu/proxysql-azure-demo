#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
python3 scripts/local-env.py
mkdir -p .local/certs artifacts
chmod 700 .local
bash scripts/local-tls.sh
scripts/docker.sh compose up -d --wait mysql redis
mysql_container="$(scripts/docker.sh compose ps -q mysql)"
scripts/docker.sh cp "$mysql_container:/var/lib/mysql/ca.pem" .local/certs/ca.pem
MYSQL_HOST=mysql MYSQL_CA_FILE=/certs/ca.pem \
  python3 scripts/with-env.py .env python3 scripts/render-proxysql.py --output .local/proxysql.cnf
scripts/docker.sh compose build --quiet node-1
scripts/docker.sh compose run --rm --no-deps -T \
  -v "$root/.local/certs:/mysql-certs:ro" --env-from-file .env \
  -e MYSQL_HOST=mysql -e MYSQL_CA_FILE=/mysql-certs/ca.pem -e MYSQL_TLS_VERIFY_IDENTITY=false \
  node-1 python /app/scripts/init-db.py
scripts/docker.sh compose up -d --wait --wait-timeout 180
scripts/configure-proxysql.sh >/dev/null
printf 'Local demo ready: http://localhost:%s (loopback only)\n' "${HTTP_PORT:-8080}"
