#!/usr/bin/env python3
"""Render only the versioned ProxySQL template, never print credentials."""

import argparse
import json
import os
from pathlib import Path
from string import Template


def render(output: Path) -> None:
    required = (
        "MYSQL_HOST",
        "MYSQL_APP_PASSWORD",
        "MYSQL_MONITOR_PASSWORD",
        "PROXYSQL_ADMIN_PASSWORD",
    )
    for key in required:
        if not os.environ.get(key):
            raise ValueError(f"Missing required environment variable: {key}")
    values = {
        "MYSQL_HOST": os.environ["MYSQL_HOST"],
        "MYSQL_DATABASE": os.getenv("MYSQL_DATABASE", "ratelimitdemo"),
        "MYSQL_APP_USER": os.getenv("MYSQL_APP_USER", "demo"),
        "MYSQL_APP_PASSWORD": os.environ["MYSQL_APP_PASSWORD"],
        "MYSQL_MONITOR_USER": os.getenv("MYSQL_MONITOR_USER", "monitor"),
        "MYSQL_MONITOR_PASSWORD": os.environ["MYSQL_MONITOR_PASSWORD"],
        "ADMIN_CREDENTIALS": f"admin:{os.environ['PROXYSQL_ADMIN_PASSWORD']}",
        "MYSQL_CA_FILE": os.getenv("MYSQL_CA_FILE", "/certs/ca.pem"),
    }
    encoded = {key: json.dumps(value) for key, value in values.items()}
    port = int(os.getenv("MYSQL_PORT", "3306"))
    if not 1 <= port <= 65535:
        raise ValueError("MYSQL_PORT must be between 1 and 65535")
    tls = os.getenv("PROXYSQL_BACKEND_TLS", "1")
    if tls not in ("0", "1"):
        raise ValueError("PROXYSQL_BACKEND_TLS must be 0 or 1")
    encoded.update(MYSQL_PORT=str(port), PROXYSQL_BACKEND_TLS=tls)
    template = Path(__file__).resolve().parents[1] / "config/proxysql.cnf.tpl"
    data = Template(template.read_text()).substitute(encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    output.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    render(parser.parse_args().output)
