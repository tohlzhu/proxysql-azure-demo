#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .local/proxy-tls
chmod 700 .local
certificate=.local/proxy-tls/proxysql-cert.pem
if [[ ! -f "$certificate" ]]; then
  umask 077
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 30 \
    -subj '/CN=proxysql-demo' \
    -addext 'subjectAltName=DNS:proxysql-1,DNS:proxysql-2' \
    -keyout .local/proxy-tls/proxysql-key.pem \
    -out "$certificate" >/dev/null 2>&1
  cp "$certificate" .local/proxy-tls/proxysql-ca.pem
  chmod 644 "$certificate" .local/proxy-tls/proxysql-ca.pem
fi
openssl x509 -checkend 3600 -noout -in "$certificate" >/dev/null || {
  echo 'Local ProxySQL certificate is expiring. Stop the demo and regenerate its local TLS files.' >&2
  exit 1
}
