#!/usr/bin/env python3
"""Initialize the isolated demo schema and least-privilege database users."""

import os
import re
import ssl
from pathlib import Path

import pymysql


def main() -> None:
    database = os.getenv("MYSQL_DATABASE", "ratelimitdemo")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", database):
        raise ValueError("Invalid MYSQL_DATABASE identifier")
    context = ssl.create_default_context(cafile=os.getenv("MYSQL_CA_FILE") or None)
    context.check_hostname = os.getenv("MYSQL_TLS_VERIFY_IDENTITY", "true").lower() == "true"
    connection = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_ADMIN_USER"],
        password=os.environ["MYSQL_ADMIN_PASSWORD"],
        ssl=context,
        autocommit=True,
        connect_timeout=10,
    )
    with connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        cursor.execute(f"USE `{database}`")
        for filename in ("schema.sql", "seed.sql"):
            path = Path(__file__).resolve().parents[1] / "sql" / filename
            for statement in path.read_text().split(";"):
                if statement.strip():
                    cursor.execute(statement)
        for role in ("APP", "MONITOR"):
            username = os.environ[f"MYSQL_{role}_USER"]
            password = os.environ[f"MYSQL_{role}_PASSWORD"]
            cursor.execute(
                "CREATE USER IF NOT EXISTS %s@'%%' "
                "IDENTIFIED WITH mysql_native_password BY %s REQUIRE SSL",
                (username, password),
            )
            cursor.execute(
                "ALTER USER %s@'%%' IDENTIFIED WITH mysql_native_password BY %s REQUIRE SSL",
                (username, password),
            )
            if role == "APP":
                cursor.execute(f"GRANT SELECT ON `{database}`.* TO %s@'%%'", (username,))
                cursor.execute(f"GRANT INSERT ON `{database}`.query_audit TO %s@'%%'", (username,))
            else:
                cursor.execute("GRANT REPLICATION CLIENT ON *.* TO %s@'%%'", (username,))
        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        if not cursor.fetchone()[1]:
            raise RuntimeError("Database initialization connection is not encrypted")
    print("Demo schema, seed data, and TLS-required users initialized.")


if __name__ == "__main__":
    main()
