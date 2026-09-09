"""Real HAProxy verifies the fixed-target off-pool query relay."""

import importlib.util
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("azure_ops", ROOT / "infra/azure_ops.py")
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("AZURE_COMPOSE_TLS_TEST") != "1", reason="opt-in real Docker test")
def test_fixed_query_relay_rejects_other_methods_paths_and_targets():
    received = []

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((self.path, self.headers.get("Host"), json.loads(body)))
            content = b'{"node_id":"fixed-backend"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    docker = shlex.split(os.getenv("DOCKER", "docker"))
    directory = ROOT / "infra/tests/.local" / ("relay-" + secrets.token_hex(6))
    directory.mkdir(parents=True, mode=0o700)
    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    container = None
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            relay_port = reservation.getsockname()[1]
        backend_endpoint = f"127.0.0.1:{backend.server_port}"
        # The test substitutes only the external dependency and loopback listen port.
        config = (
            ops.relay_config("http://10.42.1.10:8080")
            .replace("10.42.1.10:8080", backend_endpoint)
            .replace("bind :8081", f"bind 127.0.0.1:{relay_port}")
        )
        path = directory / "haproxy.cfg"
        path.write_text(config)
        path.chmod(0o644)
        container = subprocess.check_output(
            [
                *docker,
                "run",
                "-d",
                "--rm",
                "--network",
                "host",
                "--label",
                "task=azure-load-relay-validation",
                "--user",
                "99:99",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "-v",
                f"{path}:/usr/local/etc/haproxy/haproxy.cfg:ro",
                "haproxy:3.0.8",
            ],
            text=True,
        ).strip()
        base = f"http://127.0.0.1:{relay_port}"
        for attempt in range(40):
            try:
                with urllib.request.urlopen(base + "/", timeout=1):
                    pytest.fail("Relay allowed a non-query request")
            except urllib.error.HTTPError as error:
                assert error.code == 403
                break
            except OSError:
                if attempt == 39:
                    raise
                time.sleep(0.25)
        request = urllib.request.Request(
            base + "/query",
            data=b'{"query_id":"qps_demo"}',
            headers={"Content-Type": "application/json", "Host": "untrusted.example"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert json.load(response) == {"node_id": "fixed-backend"}
        assert received == [("/query", backend_endpoint, {"query_id": "qps_demo"})]
        for method, path in (
            ("GET", "/query"),
            ("POST", "/metrics"),
            ("POST", "/demo/runs"),
            ("POST", "/query?target=elsewhere"),
        ):
            request = urllib.request.Request(
                base + path,
                data=b'{"query_id":"qps_demo"}',
                method=method,
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            assert error.value.code == 403
        for request_line in (
            b"CONNECT untrusted.example:443 HTTP/1.1\r\n",
            b"POST http://untrusted.example/query HTTP/1.1\r\n",
        ):
            with socket.create_connection(("127.0.0.1", relay_port), timeout=3) as connection:
                connection.sendall(
                    request_line
                    + b"Host: untrusted.example\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                status_line = connection.recv(4096).split(b"\r\n", 1)[0]
                assert b" 403 " in status_line
        assert len(received) == 1
    finally:
        if container:
            subprocess.run([*docker, "rm", "-f", container], check=True, capture_output=True)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=3)
        shutil.rmtree(directory)
