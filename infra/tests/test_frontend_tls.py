"""Optional real-container check of the Azure ProxySQL frontend TLS contract."""

import os
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("AZURE_COMPOSE_TLS_TEST") != "1", reason="opt-in real Docker test")
def test_proxysql_frontend_certificate_and_hostname():
    docker = shlex.split(os.getenv("DOCKER", "docker"))
    directory = ROOT / "infra/tests/.local" / ("tls-" + secrets.token_hex(6))
    directory.mkdir(parents=True, mode=0o700)
    container = None
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(directory / "proxysql-key.pem"),
                "-out",
                str(directory / "proxysql-cert.pem"),
                "-subj",
                "/CN=proxysql",
                "-addext",
                "subjectAltName=DNS:proxysql",
            ],
            capture_output=True,
            check=True,
        )
        shutil.copyfile(directory / "proxysql-cert.pem", directory / "proxysql-ca.pem")
        (directory / "proxysql.cnf").write_text(
            'datadir="/var/lib/proxysql"\n'
            'admin_variables={admin_credentials="admin:' + secrets.token_urlsafe(24) + '";'
            'mysql_ifaces="127.0.0.1:6032";}\n'
            'mysql_variables={threads=1;interfaces="0.0.0.0:6033";have_ssl=true;}\n'
        )
        args = [
            *docker,
            "run",
            "-d",
            "--rm",
            "--label",
            "task=azure-frontend-tls-validation",
            "-p",
            "127.0.0.1::6033",
            "--tmpfs",
            "/var/lib/proxysql",
            "-v",
            f"{directory / 'proxysql.cnf'}:/etc/proxysql.cnf:ro",
        ]
        for name in ("proxysql-ca.pem", "proxysql-cert.pem", "proxysql-key.pem"):
            args.extend(["-v", f"{directory / name}:/var/lib/proxysql/{name}:ro"])
        args.extend(
            ["proxysql/proxysql:2.7.3", "proxysql", "-f", "--initial", "-c", "/etc/proxysql.cnf"]
        )
        container = subprocess.check_output(args, text=True).strip()
        port = int(
            subprocess.check_output([*docker, "port", container, "6033/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        context = ssl.create_default_context(cafile=str(directory / "proxysql-ca.pem"))
        for attempt in range(30):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                    header = connection.recv(4)
                    assert len(header) == 4
                    length = int.from_bytes(header[:3], "little")
                    greeting = b""
                    while len(greeting) < length:
                        greeting += connection.recv(length - len(greeting))
                    flags = 512 | 2048 | 32768
                    request = struct.pack("<IIB23s", flags, 16_777_216, 45, b"")
                    connection.sendall(len(request).to_bytes(3, "little") + b"\x01" + request)
                    with context.wrap_socket(connection, server_hostname="proxysql") as secured:
                        assert secured.cipher()
                        assert ("DNS", "proxysql") in secured.getpeercert()["subjectAltName"]
                break
            except (OSError, AssertionError):
                if attempt == 29:
                    raise
                time.sleep(0.25)
    finally:
        if container:
            subprocess.run([*docker, "rm", "-f", container], check=True, capture_output=True)
        shutil.rmtree(directory)
