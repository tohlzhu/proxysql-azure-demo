#!/usr/bin/env python3
"""Verify a private Azure demo from its off-pool VM and retrieve nonsecret evidence."""

import base64
import gzip
import importlib.util
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PREPARE = r"""
cd /opt/proxysql-demo
mkdir -p verification
chmod 700 verification
rm -f verification/distributed.json verification/local.json verification/health.json \
  verification/web.json verification/failover.json verification/fault-ready \
  verification/evidence.b64
python3 - "$1" <<'PY'
import base64,json,pathlib,sys
p=json.loads(base64.b64decode(sys.argv[1]))
pathlib.Path('verification/verify.py').write_text(p['script'])
pathlib.Path('verification/base-url').write_text(p['base_url'])
PY
docker build -q -t proxysql-demo-app:azure -f source/limiter/Dockerfile source >/dev/null
docker run --rm --user 0 --network host \
  -v "$PWD/verification:/results" proxysql-demo-app:azure \
  python /results/verify.py suites --base-url "$(cat verification/base-url)"
docker run --rm --user 0 --network host --shm-size=256m \
  -v "$PWD/verification:/results" mcr.microsoft.com/playwright/python:v1.58.0-noble \
  bash -c 'pip install --quiet playwright==1.58.0 &&
    python /results/verify.py web --base-url "$(cat /results/base-url)"'
"""

OBSERVE = r"""
cd /opt/proxysql-demo
rm -f verification/fault-ready
docker run --rm --user 0 --network host \
  -v "$PWD/verification:/results" proxysql-demo-app:azure \
  python /results/verify.py fault --base-url "$(cat verification/base-url)"
"""

FAULT = r"""
docker inspect -f '{{.State.Running}}' limiter | grep -qx true
trap 'docker start proxysql >/dev/null' EXIT
docker stop proxysql >/dev/null
docker inspect -f '{{.State.Running}}' limiter | grep -qx true
sleep 20
docker start proxysql >/dev/null
docker inspect -f '{{.State.Running}}' proxysql | grep -qx true
"""

NODE_CHECK = r"""
cd /opt/proxysql-demo
docker run --rm --network container:proxysql --env-file proxy.env \
  proxysql-demo-app:azure python /app/scripts/proxysql-admin.py --stats-only > node-stats.json
docker exec -i limiter python - <<'PY' > node-tls.json
import json,os,ssl,pymysql
context=ssl.create_default_context(cafile=os.environ['DATABASE_CA'])
with pymysql.connect(host=os.environ['DATABASE_HOST'],port=6033,
    user=os.environ['DATABASE_USER'],password=os.environ['DATABASE_PASSWORD'],
    database=os.environ['DATABASE_NAME'],ssl=context) as connection:
    frontend=connection._sock.cipher()[0]
    with connection.cursor() as cursor:
        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        backend=cursor.fetchone()[1]
        cursor.execute('SELECT COUNT(*) FROM customers')
        count=cursor.fetchone()[0]
    assert frontend and backend and count >= 3
    print(json.dumps({'frontend_cipher':frontend,'backend_cipher':backend,'customers':count}))
PY
python3 - <<'PY'
import base64,gzip,json,pathlib
result={'stats':json.loads(pathlib.Path('node-stats.json').read_text()),
        'tls':json.loads(pathlib.Path('node-tls.json').read_text())}
runtime=sorted(
    (int(r['rule_id']),int(r['active'])) for r in result['stats']['runtime_mysql_query_rules'])
disk=sorted(
    (int(r['rule_id']),int(r['active'])) for r in result['stats']['disk_mysql_query_rules'])
assert runtime==disk==[(1,1),(10,1)]
result['persistence']={'runtime':runtime,'disk':disk,'matched':True}
print('RESULT='+base64.b64encode(gzip.compress(json.dumps(result).encode())).decode())
PY
rm -f node-stats.json node-tls.json
"""

PACKAGE = r"""
cd /opt/proxysql-demo/verification
python3 - <<'PY'
import base64,gzip,json,pathlib
files={p.name:json.loads(p.read_text()) for p in pathlib.Path('.').glob('*.json')}
expected={'distributed.json','local.json','health.json','web.json','failover.json'}
assert expected.issubset(files), 'Missing required verification evidence'
encoded=base64.b64encode(gzip.compress(json.dumps(files).encode(),mtime=0)).decode()
pathlib.Path('evidence.b64').write_text(encoded)
print('SIZE='+str(len(encoded)))
PY
"""

CHUNK = r"""
python3 - "$1" <<'PY'
import base64,json,pathlib,sys
p=json.loads(base64.b64decode(sys.argv[1]))
assert isinstance(p['offset'],int) and 0<=p['offset']<=2000000
text=pathlib.Path('/opt/proxysql-demo/verification/evidence.b64').read_text()
print('CHUNK='+text[p['offset']:p['offset']+2800])
PY
"""


def marked(output: str | None, prefix: str) -> str:
    rows = [line[len(prefix) :] for line in (output or "").splitlines() if line.startswith(prefix)]
    if len(rows) != 1:
        raise RuntimeError(f"Expected one complete remote {prefix} response")
    return rows[0]


def main() -> None:
    spec = importlib.util.spec_from_file_location("demo_azure_ops", ROOT / "infra/azure_ops.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Missing Azure operation module")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    ops.context()
    outputs = ops.outputs()
    resources = ops.state_scope(outputs)
    redis_vm = outputs["redis_vm_name"]
    destination = ROOT / "artifacts/azure"
    destination.mkdir(parents=True, exist_ok=True)
    print("Running both limiter modes and Chromium inside the private VNet.", flush=True)
    ops.managed_command(
        outputs,
        redis_vm,
        PREPARE,
        {
            "script": (ROOT / "scripts/azure-remote-verify.py").read_text(),
            "base_url": outputs["private_http_url"],
        },
        timeout=1200,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        observer = executor.submit(ops.managed_command, outputs, redis_vm, OBSERVE, timeout=600)
        try:
            time.sleep(5)
            ready = ops.managed_command(
                outputs,
                redis_vm,
                "for attempt in $(seq 1 90); do\n"
                "  if test -s /opt/proxysql-demo/verification/fault-ready; then\n"
                "    echo OBSERVER_READY; exit 0\n"
                "  fi\n  sleep 2\ndone\nexit 1\n",
                timeout=240,
                capture_output=True,
            )
            if "OBSERVER_READY" not in (ready or ""):
                raise RuntimeError("Fault observer did not start; no fault injected")
            observer_running = not observer.done()
            if not observer_running:
                observer.result()
                raise RuntimeError("Fault observer exited before injection")
            print(
                "Injecting a bounded ProxySQL-only outage; automatic restart is armed.", flush=True
            )
            ops.managed_command(outputs, outputs["node_vm_names"]["node-1"], FAULT, timeout=180)
        finally:
            observer.result()

    for node, vm in outputs["node_vm_names"].items():
        output = ops.managed_command(outputs, vm, NODE_CHECK, timeout=180, capture_output=True)
        proof = json.loads(gzip.decompress(base64.b64decode(marked(output, "RESULT="))))
        (destination / f"{node}.json").write_text(json.dumps(proof, indent=2) + "\n")
    output = ops.managed_command(outputs, redis_vm, PACKAGE, timeout=120, capture_output=True)
    size = int(marked(output, "SIZE="))
    if not 0 < size <= 2_000_000:
        raise RuntimeError("Remote verification evidence exceeds its transfer bound")
    chunks = []
    for offset in range(0, size, 2800):
        output = ops.managed_command(
            outputs,
            redis_vm,
            CHUNK,
            {"offset": offset},
            timeout=120,
            capture_output=True,
        )
        chunks.append(marked(output, "CHUNK="))
    encoded = "".join(chunks)
    if len(encoded) != size:
        raise RuntimeError("Remote evidence transfer was truncated")
    evidence = json.loads(gzip.decompress(base64.b64decode(encoded, validate=True)))
    for name, document in evidence.items():
        if Path(name).name != name or not name.endswith(".json"):
            raise RuntimeError("Unsafe remote result filename")
        (destination / name).write_text(json.dumps(document, indent=2) + "\n")
    for resource in resources:
        if resource["type"] == "azurerm_lb" and resource["name"] == "private":
            metrics = ops.az(
                [
                    "monitor",
                    "metrics",
                    "list",
                    "--resource",
                    resource["values"]["id"],
                    "--metric",
                    "DipAvailability",
                    "--filter",
                    "BackendIPAddress eq '*'",
                    "--interval",
                    "PT1M",
                    "--aggregation",
                    "Average",
                    "--query",
                    "value[].{name:name.value,timeseries:timeseries}",
                ]
            )
            (destination / "lb-probes.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(f"Azure verification complete; original evidence saved to {destination}.", flush=True)


if __name__ == "__main__":
    main()
