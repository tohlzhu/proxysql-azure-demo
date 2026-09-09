#!/usr/bin/env python3
"""Generate credentials for this local demo only; never overwrite an existing environment."""

import os
import secrets
from pathlib import Path


def main() -> None:
    path = Path(".env")
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError(".env must have mode 600; run chmod 600 .env")
        return
    values = {
        "COMPOSE_PROJECT_NAME": "proxysql-demo",
        "MYSQL_DATABASE": "ratelimitdemo",
        "MYSQL_ADMIN_USER": "root",
        "MYSQL_APP_USER": "demo",
        "MYSQL_MONITOR_USER": "monitor",
        "LIMITER_MODE": "distributed",
        "HTTP_PORT": "8080",
        "REDIS_PORT": "16379",
    }
    for key in (
        "MYSQL_ADMIN_PASSWORD",
        "MYSQL_APP_PASSWORD",
        "MYSQL_MONITOR_PASSWORD",
        "PROXYSQL_ADMIN_PASSWORD",
        "REDIS_PASSWORD",
    ):
        values[key] = secrets.token_hex(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.writelines(f"{key}={value}\n" for key, value in values.items())
    print("Created .env with fresh local-only credentials (mode 600).")


if __name__ == "__main__":
    main()
