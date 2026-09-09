#!/usr/bin/env python3
"""Bounded Azure operations; credentials and API identifiers stay out of logs."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import io
import ipaddress
import json
import os
import re
import secrets
import signal
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra/terraform"
SUBSCRIPTION = "ME-MngEnvMCAP012397-zhuhonglei-1"
RG = "rg-dev2"
LOCATION = "japaneast"
KNOWN_SECRETS: list[str] = []


def safe(text: str) -> str:
    for secret in KNOWN_SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
        "[REDACTED-ID]",
        text,
    )
    return text


def run(args: list[str], *, data: str | bytes | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        input=data,
        capture_output=True,
        text=not isinstance(data, bytes),
        check=False,
    )
    stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
    if result.returncode:
        # CLI failure may contain the request body. Never echo az errors for writes.
        if args[:2] == ["az", "rest"] and "get" not in args:
            raise RuntimeError("Azure write failed; inspect the scoped resource status in Azure.")
        raise RuntimeError(safe(f"{args[0]} failed:\n{stderr or stdout}")[:4000])
    return stdout


def az(args: list[str], *, body: dict | None = None) -> dict | list:
    command = ["az", *args, "--only-show-errors", "-o", "json"]
    if body is not None:
        command.extend(["--body", "@/dev/stdin"])
    result = run(command, data=json.dumps(body) if body is not None else None)
    return json.loads(result) if result.strip() else {}


def tf(*args: str, data: str | None = None) -> str:
    return run(["terraform", f"-chdir={TF}", *args], data=data)


def context() -> str:
    account = az(["account", "show", "--query", "{name:name,id:id,state:state}"])
    if account["name"] != SUBSCRIPTION or account["state"] != "Enabled":
        raise RuntimeError(
            "Current subscription must match the documented enabled target; not switching."
        )
    subscription_id = account["id"]
    KNOWN_SECRETS.append(subscription_id)
    os.environ["ARM_SUBSCRIPTION_ID"] = subscription_id
    os.environ["ARM_RESOURCE_PROVIDER_REGISTRATIONS"] = "none"
    os.environ["TF_IN_AUTOMATION"] = "true"
    group = az(
        [
            "group",
            "show",
            "-n",
            RG,
            "--subscription",
            subscription_id,
            "--query",
            "{name:name,location:location}",
        ]
    )
    if group != {"name": RG, "location": LOCATION}:
        raise RuntimeError("Authorized shared resource group is not in Japan East.")
    print(f"Context: {SUBSCRIPTION}; {RG}; {LOCATION}. No provider auto-registration.")
    return subscription_id


def ephemeral_public_key() -> str:
    private = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        capture_output=True,
        check=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "rsa", "-RSAPublicKey_out", "-outform", "DER"],
        input=private,
        capture_output=True,
        check=True,
    ).stdout

    def item(offset: int) -> tuple[bytes, int]:
        length = der[offset + 1]
        start = offset + 2
        if length & 128:
            size = length & 127
            length = int.from_bytes(der[start : start + size], "big")
            start += size
        return der[start : start + length], start

    _, start = item(0)
    modulus, offset = item(start)
    exponent, _ = item(offset + len(modulus))
    fields = [b"ssh-rsa", exponent, modulus]
    blob = b"".join(struct.pack(">I", len(value)) + value for value in fields)
    return "ssh-rsa " + base64.b64encode(blob).decode() + " generated-no-ssh-access"


def prepare_inputs(*, validation: bool) -> None:
    os.environ.setdefault("TF_VAR_ssh_public_key", ephemeral_public_key())
    if "TF_VAR_mysql_admin_password" not in os.environ:
        os.environ["TF_VAR_mysql_admin_password"] = "Aa1!" + secrets.token_urlsafe(28)
    KNOWN_SECRETS.append(os.environ["TF_VAR_mysql_admin_password"])
    if validation:
        print(
            "Validation only: missing credentials replaced IN MEMORY; not deployment credentials."
        )
    # Persisted keys must remain stable across later plans to avoid replacing VMs.
    current = json.loads(tf("show", "-json"))
    if current.get("values"):
        for resource in resources(current.get("values", {})):
            if resource["type"] == "azurerm_linux_virtual_machine":
                keys = resource["values"].get("admin_ssh_key", [])
                if keys:
                    os.environ["TF_VAR_ssh_public_key"] = keys[0]["public_key"]
            if resource["type"] == "azurerm_mysql_flexible_server":
                value = resource["values"].get("administrator_password")
                if value:
                    os.environ["TF_VAR_mysql_admin_password"] = value
                    KNOWN_SECRETS.append(value)


def variables() -> dict:
    expression = (
        "jsonencode({project_id=var.project_id,task_id=var.task_id,"
        "node_vm_size=var.node_vm_size,redis_vm_size=var.redis_vm_size,"
        "node_zones=var.node_zones,mysql_sku=var.mysql_sku,"
        "mysql_ha_enabled=var.mysql_ha_enabled,"
        "observability_enabled=var.observability_enabled})"
    )
    return json.loads(json.loads(tf("console", "-no-color", data=expression + "\n")))


def select_zones(config: dict) -> None:
    names = sorted({config["node_vm_size"], config["redis_vm_size"]})
    query = (
        "[?"
        + " || ".join("name=='" + name + "'" for name in names)
        + ("].{name:name,zones:locationInfo[0].zones,restrictions:restrictions}")
    )
    if not all(re.fullmatch(r"Standard_[A-Za-z0-9_]+", name) for name in names):
        raise RuntimeError("Invalid VM SKU name.")
    skus = az(
        [
            "vm",
            "list-skus",
            "-l",
            LOCATION,
            "--resource-type",
            "virtualMachines",
            "--all",
            "--subscription",
            os.environ["ARM_SUBSCRIPTION_ID"],
            "--query",
            query,
        ]
    )
    by_name = {sku["name"]: sku for sku in skus}
    for name in names:
        if name not in by_name:
            raise RuntimeError(f"VM SKU {name} not advertised in Japan East.")
        if any(r["type"] == "Location" for r in by_name[name]["restrictions"]):
            raise RuntimeError(
                f"VM SKU {name} unavailable for this subscription; choose an available SKU."
            )
    node = by_name[config["node_vm_size"]]
    restricted = {
        zone
        for restriction in node["restrictions"]
        if restriction["type"] == "Zone"
        for zone in restriction.get("restrictionInfo", {}).get("zones", [])
    }
    available = sorted(set(node.get("zones") or []) - restricted)
    requested = config["node_zones"]
    if requested and not set(requested).issubset(available):
        raise RuntimeError("Explicit node_zones include a subscription-restricted zone.")
    zones = requested or (available[:2] if len(available) >= 2 else [])
    os.environ["TF_VAR_node_zones"] = json.dumps(zones)
    print(
        "VM placement: "
        + (
            f"zones {', '.join(zones)}"
            if zones
            else "AUTOMATIC DOWNGRADE to regional placement; no zonal HA guarantee."
        )
    )
    print(f"VM SKUs: 2 x {config['node_vm_size']}; Redis 1 x {config['redis_vm_size']}.")
    required = ["Microsoft.Network", "Microsoft.Compute", "Microsoft.DBforMySQL"]
    if config["observability_enabled"]:
        required.append("Microsoft.OperationalInsights")
    states = az(
        [
            "provider",
            "list",
            "--subscription",
            os.environ["ARM_SUBSCRIPTION_ID"],
            "--query",
            "[?"
            + " || ".join(f"namespace=='{n}'" for n in required)
            + "].{namespace:namespace,state:registrationState}",
        ]
    )
    if {p["namespace"] for p in states if p["state"] == "Registered"} != set(required):
        raise RuntimeError(
            "Required providers are not preregistered; no subscription registration attempted."
        )
    print("Required resource providers: Registered (read-only check).")


def resources(values: dict) -> list[dict]:
    result = []

    def walk(module: dict) -> None:
        result.extend(module.get("resources", []))
        for child in module.get("child_modules", []):
            walk(child)

    walk(values.get("root_module", {}))
    return result


def inspect_plan(plan: dict, project: str, *, destroying: bool = False) -> dict:
    counts = {"add": 0, "change": 0, "destroy": 0}
    for change in plan.get("resource_changes", []):
        if change["mode"] != "managed":
            continue
        actions = change["change"]["actions"]
        counts["add"] += "create" in actions
        counts["change"] += "update" in actions
        counts["destroy"] += "delete" in actions
        if change["type"] == "azurerm_resource_group":
            raise RuntimeError("Resource group management is forbidden.")
        if "delete" in actions and not destroying:
            raise RuntimeError(
                "Plan contains deletion/replacement; refusing unexpected destructive changes."
            )
    for resource in resources(plan.get("planned_values", {})):
        if resource["mode"] != "managed":
            continue
        values = resource["values"]
        tags = values.get("tags")
        if tags is not None and (
            tags.get("project") != project
            or tags.get("owner") != "agent"
            or tags.get("autodelete") != "true"
            or not tags.get("task")
        ):
            raise RuntimeError("Managed resource lacks required ownership tags.")
        if resource["type"] == "azurerm_mysql_flexible_server":
            # The provider exposes public_network_access_enabled as COMPUTED.
            # Both private network references must exist in configuration even before IDs are known.
            if values.get("public_network_access_enabled") is True:
                raise RuntimeError("Public database access is forbidden.")
        if resource["type"] == "azurerm_mysql_flexible_server_firewall_rule":
            raise RuntimeError("Database public firewall resources are forbidden.")
    output_changes = plan.get("output_changes", {})
    for name, change in output_changes.items():
        if change.get("after_sensitive") or re.search(
            "password|secret|token|tenant|subscription", name, re.I
        ):
            raise RuntimeError("Secret or identifier output is forbidden.")
    server_changes = [
        c
        for c in plan.get("resource_changes", [])
        if c["type"] == "azurerm_mysql_flexible_server" and c["mode"] == "managed"
    ]
    if not destroying:
        for change in server_changes:
            after = change["change"]["after"]
            unknown = change["change"].get("after_unknown", {})
            for key in ("delegated_subnet_id", "private_dns_zone_id"):
                if not after.get(key) and not unknown.get(key):
                    raise RuntimeError("MySQL private network/DNS configuration is required.")
    elif counts["add"] or counts["change"]:
        raise RuntimeError("A destroy plan must not create or update managed resources.")
    print(
        f"SAFE plan summary: {counts['add']} add, "
        f"{counts['change']} change, {counts['destroy']} destroy."
    )
    return counts


@contextlib.contextmanager
def planned(*, validation: bool, destroying: bool = False):
    os.umask(0o077)
    tf("init", "-input=false", "-no-color")
    prepare_inputs(validation=validation)
    config = variables()
    if not destroying:
        select_zones(config)
    plan_path = TF / f".operation-{os.getpid()}.tfplan"
    try:
        args = ["plan", "-input=false", "-no-color", "-lock-timeout=60s", f"-out={plan_path}"]
        if destroying:
            args.append("-destroy")
        tf(*args)
        plan = json.loads(tf("show", "-json", str(plan_path)))
        inspect_plan(plan, config["project_id"], destroying=destroying)
        yield plan_path, plan, config
    finally:
        plan_path.unlink(missing_ok=True)


def outputs() -> dict:
    raw = json.loads(tf("output", "-json"))
    if not raw or any(value["sensitive"] for value in raw.values()):
        raise RuntimeError("Expected nonsecret Terraform outputs from an existing deployment.")
    result = {key: value["value"] for key, value in raw.items()}
    if result["resource_group_name"] != RG or result["location"] != LOCATION:
        raise RuntimeError("Terraform outputs are outside authorized scope.")
    return result


def state_scope(
    out: dict,
    *,
    state: dict | None = None,
    require_complete: bool = True,
    allow_unlabeled_disks: bool = False,
) -> list[dict]:
    state = state if state is not None else json.loads(tf("show", "-json"))
    managed = [r for r in resources(state.get("values", {})) if r["mode"] == "managed"]
    if not managed:
        raise RuntimeError("No managed resources in this exact state.")
    prefix = f"/subscriptions/{os.environ['ARM_SUBSCRIPTION_ID']}/resourceGroups/{RG}/"
    tagged: list[dict] = []
    for resource in managed:
        value = resource["values"]
        identifier = value.get("id", "")
        # Terraform associations encode an exact parent ID plus a provider separator.
        if resource["type"] == "azurerm_resource_group" or not identifier.lower().startswith(
            prefix.lower()
        ):
            raise RuntimeError("State contains an unmanaged-scope resource; refusing operation.")
        if "tags" in value:
            tags = value.get("tags") or {}
            if (
                tags.get("project") != out["project_id"]
                or tags.get("owner") != "agent"
                or tags.get("autodelete") != "true"
                or not tags.get("task")
            ):
                raise RuntimeError("State ownership tags do not match this project.")
            live = az(["resource", "show", "--ids", identifier, "--query", "{name:name,tags:tags}"])
            actual = live.get("tags") or {}
            if any(
                actual.get(key) != tags.get(key)
                for key in ("project", "owner", "task", "autodelete")
            ):
                raise RuntimeError("Live ownership tags no longer match state.")
            tagged.append(resource)
            if resource["type"] == "azurerm_linux_virtual_machine":
                disk_id = f"{prefix}providers/Microsoft.Compute/disks/{value['os_disk'][0]['name']}"
                disk = az(
                    [
                        "resource",
                        "show",
                        "--ids",
                        disk_id,
                        "--query",
                        "{managedBy:managedBy,tags:tags}",
                    ]
                )
                if str(disk.get("managedBy", "")).lower() != identifier.lower():
                    raise RuntimeError("VM OS disk ownership does not match exact state.")
                disk_tags = disk.get("tags") or {}
                missing = False
                for key in ("project", "owner", "task", "autodelete"):
                    if disk_tags.get(key) == tags.get(key):
                        continue
                    if key not in disk_tags and allow_unlabeled_disks:
                        missing = True
                        continue
                    raise RuntimeError("VM OS disk lacks matching live ownership tags.")
                if missing:
                    print(
                        f"  {value['os_disk'][0]['name']}: "
                        "exact attached OS disk from partial apply; "
                        "ownership tags must be repaired after confirmation before deletion."
                    )
    # Non-taggable child resources/associations must reference a whitelisted parent.
    parents = [r["values"]["id"].lower() for r in tagged]
    for resource in managed:
        if "tags" not in resource["values"]:
            identifier = resource["values"]["id"].lower()
            if not any(
                identifier.startswith(parent + "/") or identifier.startswith(parent + "|")
                for parent in parents
            ):
                raise RuntimeError("Non-taggable state resource has no exact owned parent.")
    if require_complete:
        expected_vms = set(out["node_vm_names"].values()) | {out["redis_vm_name"]}
        owned_vms = {
            resource["values"]["name"]
            for resource in managed
            if resource["type"] == "azurerm_linux_virtual_machine"
        }
        if expected_vms != owned_vms or len(expected_vms) != 3:
            raise RuntimeError("VM outputs do not match the three exact state-owned machines.")
    return managed


def cleanup_context() -> tuple[dict, dict]:
    state = json.loads(tf("show", "-json"))
    projects = {
        resource["values"]["tags"].get("project")
        for resource in resources(state.get("values", {}))
        if resource["mode"] == "managed" and resource["values"].get("tags")
    }
    if len(projects) != 1 or not next(iter(projects), None):
        raise RuntimeError("Cleanup requires one unambiguous owned project in the exact state.")
    return {"project_id": projects.pop()}, state


def confirm(expected: str, message: str) -> None:
    print(message)
    if input(f"Type exactly {expected}: ").strip() != expected:
        raise RuntimeError("Confirmation did not match; no Azure modification performed.")


def cost_summary(config: dict) -> None:
    print(f"MySQL {config['mysql_sku']} (20 GiB, 7-day backup); HA={config['mysql_ha_enabled']}.")
    print(
        "Billable: 3 VMs + 30-GiB disks, MySQL, 2 Standard LBs, "
        "one IPv4, outbound bytes; optional logs."
    )
    print("Egress: explicit public LB SNAT, not a NAT Gateway. Redis remains an economical SPOF.")
    print("No live price quote: keep this demo to hours; a retained deployment may exceed $200.")
    print("If anticipated cumulative charges exceed your authorized budget, do not type DEPLOY.")
    print(
        f"State: {TF / 'terraform.tfstate'} "
        "(sensitive admin password; protect/back up, never commit)."
    )
    print(
        "Cleanup: scripts/destroy.sh (exact state + live ownership checks; never deletes rg-dev2)."
    )


def managed_command(
    out: dict,
    vm: str,
    script: str,
    payload: dict | None = None,
    *,
    timeout: int = 1200,
    capture_output: bool = False,
) -> str | None:
    allowed = set(out["node_vm_names"].values()) | {out["redis_vm_name"]}
    if vm not in allowed:
        raise RuntimeError("VM is not one of the exact Terraform output names.")
    command_name = "demo-operation-" + secrets.token_hex(6)
    base = (
        f"https://management.azure.com/subscriptions/{os.environ['ARM_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{RG}/providers/Microsoft.Compute/virtualMachines/{vm}"
        f"/runCommands/{command_name}"
    )
    url = base + "?api-version=2024-11-01"
    encoded = base64.b64encode(json.dumps(payload or {}).encode()).decode()
    body = {
        "location": LOCATION,
        "tags": {
            "owner": "agent",
            "project": out["project_id"],
            "task": "demo-publish",
            "autodelete": "true",
        },
        "properties": {
            "source": {
                "script": "#!/usr/bin/env bash\nset -euo pipefail\n"
                'set -- "$payload"\nunset payload\n' + script
            },
            "protectedParameters": [{"name": "payload", "value": encoded}],
            "timeoutInSeconds": timeout,
            "asyncExecution": False,
            "treatFailureAsDeploymentFailure": True,
        },
    }
    deadline = time.monotonic() + timeout + 180
    completed = False
    az(["rest", "--method", "put", "--url", url], body=body)
    try:
        while time.monotonic() < deadline:
            status = az(
                [
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    url + "&$expand=instanceView",
                    "--query",
                    "{state:properties.provisioningState,view:properties.instanceView}",
                ]
            )
            view = status.get("view") or {}
            if status.get("state") == "Failed" or view.get("executionState") in (
                "Failed",
                "TimedOut",
            ):
                completed = True
                raise RuntimeError(
                    f"Managed Run Command failed on {vm}; no secret-bearing output printed."
                )
            if view.get("executionState") == "Succeeded":
                completed = True
                if view.get("exitCode", 0) != 0:
                    raise RuntimeError(f"Managed Run Command returned nonzero on {vm}.")
                return safe(str(view.get("output") or "")) if capture_output else None
            time.sleep(5)
        raise RuntimeError(
            f"Managed Run Command timed out on {vm}; inspect exact VM before retrying."
        )
    finally:
        if completed:
            az(["rest", "--method", "delete", "--url", url])
        else:
            print(
                f"Run Command {command_name} on {vm} still has uncertain execution state; "
                "left intact to avoid interrupting an accepted operation.",
                file=sys.stderr,
            )


def source_archive() -> str:
    buffer = io.BytesIO()
    files = [
        ROOT / "limiter/Dockerfile",
        ROOT / "limiter/pyproject.toml",
        ROOT / "config/limits.yaml",
        ROOT / "config/proxysql.cnf.tpl",
    ]
    for directory in ("limiter/src", "limiter/static", "limiter/templates", "sql"):
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    files.extend(
        ROOT / "scripts" / name
        for name in (
            "init-db.sh",
            "init-db.py",
            "configure-proxysql.sh",
            "proxysql-admin.py",
            "render-proxysql.py",
        )
    )
    files.extend(
        [
            ROOT / "infra/templates/compose-node.yaml.tpl",
            ROOT / "infra/templates/compose-redis.yaml.tpl",
            ROOT / "infra/templates/haproxy-relay.cfg.tpl",
        ]
    )
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(set(files)):
            if "__pycache__" not in path.parts and path.suffix != ".pyc":
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(ROOT)
                ):
                    raise RuntimeError(
                        f"Missing or symlinked deployment source: {path.relative_to(ROOT)}"
                    )
                archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    if len(encoded) > 2_000_000:
        raise RuntimeError(
            "Allowlisted source archive exceeds 2 MB; refusing excessive Run Command requests."
        )
    return encoded


def relay_config(private_http_url: str) -> str:
    parsed = urllib.parse.urlsplit(private_http_url)
    try:
        address = ipaddress.IPv4Address(parsed.hostname or "")
    except ipaddress.AddressValueError as exc:
        raise RuntimeError("Relay requires the Terraform internal LB IPv4 endpoint.") from exc
    if (
        not address.is_private
        or address.is_loopback
        or private_http_url != f"http://{address}:8080"
    ):
        raise RuntimeError("Relay target must be only the Terraform private HTTP LB on port 8080.")
    template = (ROOT / "infra/templates/haproxy-relay.cfg.tpl").read_text()
    return Template(template).substitute(LB_ENDPOINT=f"{address}:8080")


UPLOAD = r"""
python3 - "$1" <<'PY'
import base64,json,pathlib,sys
p=json.loads(base64.b64decode(sys.argv[1]))
root=pathlib.Path('/opt/proxysql-demo')
root.mkdir(mode=0o700,exist_ok=True)
path=root/'source.tar.gz.b64'
with path.open('w' if p['first'] else 'a') as stream:
    stream.write(p['chunk'])
path.chmod(0o600)
PY
"""

PUBLISH = r"""
for attempt in $(seq 1 90); do
  test -f /opt/proxysql-demo/.bootstrap-ready && break
  sleep 10
done
test -f /opt/proxysql-demo/.bootstrap-ready
cd /opt/proxysql-demo
base64 -d source.tar.gz.b64 > source.tar.gz
# Only this script's generated source copy is replaced; data volumes are retained.
rm -rf -- /opt/proxysql-demo/source
mkdir -p source
tar -xzf source.tar.gz -C source
rm -f source.tar.gz source.tar.gz.b64
python3 - "$1" <<'PY'
import base64,json,os,pathlib,sys
p=json.loads(base64.b64decode(sys.argv[1]))
root=pathlib.Path('/opt/proxysql-demo')
os.umask(0o077)
def envfile(name, values):
    # Docker env_file raw format prevents dollar expansion in credentials.
    (root/name).write_text('\n'.join(f'{k}={v}' for k,v in values.items())+'\n')
if p['role']=='redis':
    password=p['redis_password']
    (root/'redis.conf').write_text(
        'bind 0.0.0.0\nprotected-mode yes\nport 6379\nappendonly yes\n'
        'maxmemory 384mb\nmaxmemory-policy noeviction\nrequirepass '+password+'\n')
    (root/'redis.conf').chmod(0o644)
    (root/'haproxy-relay.cfg').write_text(p['relay_config'])
    (root/'haproxy-relay.cfg').chmod(0o644)
else:
    envfile('runtime.env',p['runtime'])
    envfile('admin.env',p['admin'])
    envfile('proxy.env',p['proxy'])
PY
role="$(python3 - "$1" <<'PY'
import base64,json,sys
print(json.loads(base64.b64decode(sys.argv[1]))["role"])
PY
)"
if [[ "$role" == redis ]]; then
  cp source/infra/templates/compose-redis.yaml.tpl compose.yaml
  docker compose up -d --force-recreate
  docker exec redis sh -c '
    REDISCLI_AUTH="$(sed -n "s/^requirepass //p" /usr/local/etc/redis/redis.conf)" redis-cli ping
  ' | grep -qx PONG
  docker exec load-relay haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null
  python3 - <<'PY'
import time,urllib.error,urllib.request
for attempt in range(30):
    try:
        urllib.request.urlopen('http://127.0.0.1:8081/',timeout=2)
    except urllib.error.HTTPError as error:
        if error.code==403:
            break
        raise
    except OSError:
        time.sleep(1)
    else:
        raise RuntimeError('Load relay did not reject a non-query request.')
else:
    raise RuntimeError('Load relay did not become responsive.')
print('Redis ready; fixed-target load relay running and denying non-query requests.')
PY
  exit 0
fi
trap 'rm -f admin.env' EXIT
mkdir -p certs
chmod 0755 certs
cp /etc/ssl/certs/ca-certificates.crt certs/ca.pem
if ! openssl x509 -checkend 86400 -noout -in certs/proxysql-cert.pem >/dev/null 2>&1; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -keyout certs/proxysql-key.pem -out certs/proxysql-cert.pem \
    -subj "/CN=proxysql" -addext "subjectAltName=DNS:proxysql" >/dev/null 2>&1
  cp certs/proxysql-cert.pem certs/proxysql-ca.pem
fi
chmod 0600 certs/proxysql-key.pem
chmod 0644 certs/ca.pem certs/proxysql-ca.pem certs/proxysql-cert.pem
docker build -q -t proxysql-demo-app:azure -f source/limiter/Dockerfile source >/dev/null
docker run --rm --user 0 --env-file proxy.env -v "$PWD:/deploy" \
  proxysql-demo-app:azure python /app/scripts/render-proxysql.py --output /deploy/proxysql.cnf
docker run --rm --network host --env-file admin.env -v "$PWD/certs:/certs:ro" \
  proxysql-demo-app:azure bash /app/scripts/init-db.sh
cp source/infra/templates/compose-node.yaml.tpl compose.yaml
docker compose up -d --force-recreate proxysql
for attempt in $(seq 1 30); do
  if docker run --rm --network container:proxysql --env-file proxy.env \
      proxysql-demo-app:azure bash /app/scripts/configure-proxysql.sh \
      --host 127.0.0.1 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker run --rm --network container:proxysql --env-file proxy.env \
  proxysql-demo-app:azure bash /app/scripts/configure-proxysql.sh --host 127.0.0.1
docker compose up -d --force-recreate app
for attempt in $(seq 1 60); do
  if python3 -c \
    'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8080/readyz",timeout=3)' \
    2>/dev/null; then
    echo 'Node application, ProxySQL and database ready.'
    exit 0
  fi
  sleep 2
done
echo 'Node readiness failed.' >&2
exit 1
"""


def publish(out: dict) -> None:
    state_scope(out)
    state = json.loads(tf("show", "-json"))
    mysql = next(
        r["values"]
        for r in resources(state["values"])
        if r["type"] == "azurerm_mysql_flexible_server"
    )
    admin_password = os.getenv("MYSQL_ADMIN_PASSWORD") or mysql.get("administrator_password")
    if not admin_password:
        raise RuntimeError(
            "MySQL admin password unavailable; provide it through approved environment injection."
        )
    if any(character in admin_password for character in ("\r", "\n", "\0")):
        raise RuntimeError("Admin password cannot contain environment-file control characters.")
    password_names = (
        "MYSQL_APP_PASSWORD",
        "MYSQL_MONITOR_PASSWORD",
        "REDIS_PASSWORD",
        "PROXYSQL_ADMIN_PASSWORD",
    )
    credentials = {
        name: os.environ.get(name) or secrets.token_urlsafe(32) for name in password_names
    }
    # Restrict to a portable env/config alphabet rather than interpolating untrusted syntax.
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) for value in credentials.values()):
        raise RuntimeError(
            "Runtime passwords need 16-128 alphanumeric, dash or underscore characters; "
            "generated defaults satisfy this."
        )
    KNOWN_SECRETS.extend([admin_password, *credentials.values()])
    if os.getenv("LIMITER_MODE", "distributed") not in ("local", "distributed"):
        raise RuntimeError("LIMITER_MODE must be local or distributed.")
    archive = source_archive()
    rendered_relay = relay_config(out["private_http_url"])
    machines = [("redis", out["redis_vm_name"]), *out["node_vm_names"].items()]
    for role, vm in machines:
        print(
            f"Publishing allowlisted local source to {vm}; "
            "rebuilding pinned application image on VM."
        )
        for offset in range(0, len(archive), 24000):
            managed_command(
                out, vm, UPLOAD, {"first": offset == 0, "chunk": archive[offset : offset + 24000]}
            )
        payload: dict = {"role": role, "redis_password": credentials["REDIS_PASSWORD"]}
        if role == "redis":
            payload["relay_config"] = rendered_relay
        else:
            proxy = {
                "MYSQL_HOST": out["mysql_host"],
                "MYSQL_PORT": "3306",
                "MYSQL_DATABASE": "ratelimitdemo",
                "MYSQL_APP_USER": "demo",
                "MYSQL_APP_PASSWORD": credentials["MYSQL_APP_PASSWORD"],
                "MYSQL_MONITOR_USER": "monitor",
                "MYSQL_MONITOR_PASSWORD": credentials["MYSQL_MONITOR_PASSWORD"],
                "PROXYSQL_ADMIN_PASSWORD": credentials["PROXYSQL_ADMIN_PASSWORD"],
                "PROXYSQL_BACKEND_TLS": "1",
                "MYSQL_CA_FILE": "/certs/ca.pem",
            }
            payload["proxy"] = proxy
            payload["admin"] = {
                **proxy,
                "MYSQL_ADMIN_USER": out["mysql_admin_user"],
                "MYSQL_ADMIN_PASSWORD": admin_password,
            }
            payload["runtime"] = {
                "DATABASE_HOST": "proxysql",
                "DATABASE_PORT": "6033",
                "DATABASE_USER": "demo",
                "DATABASE_PASSWORD": credentials["MYSQL_APP_PASSWORD"],
                "DATABASE_NAME": "ratelimitdemo",
                "DATABASE_TLS": "true",
                "DATABASE_CA": "/certs/proxysql-ca.pem",
                "REDIS_URL": (
                    f"redis://:{urllib.parse.quote(credentials['REDIS_PASSWORD'], safe='')}"
                    f"@{out['redis_private_ip']}:6379/0"
                ),
                "LIMITER_MODE": os.getenv("LIMITER_MODE", "distributed"),
                "LIMITER_NODE_ID": role,
                "LIMITER_TARGET_URLS": ",".join(
                    f"http://{ip}:8080" for ip in out["node_private_ips"].values()
                ),
                "LIMITER_LOAD_URL": f"http://{out['redis_private_ip']}:8081",
                "LIMITER_PUBLIC_URL": out["http_url"],
                "LIMITER_CONFIG": "/app/config/limits.yaml",
            }
        managed_command(out, vm, PUBLISH, payload, timeout=1800)
    managed_command(
        out,
        out["redis_vm_name"],
        r"""
python3 - <<'PY'
import json,time,urllib.error,urllib.request
seen=set()
for attempt in range(40):
    request=urllib.request.Request(
        'http://127.0.0.1:8081/query',
        data=b'{"query_id":"qps_demo","limiter_mode":"distributed"}',
        headers={'Content-Type':'application/json','Connection':'close'})
    try:
        with urllib.request.urlopen(request,timeout=3) as response:
            result=json.load(response)
            if response.status==200:
                seen.add(result.get('node_id'))
    except (OSError,ValueError):
        pass
    if {'node-1','node-2'}.issubset(seen):
        print('Off-pool relay and private LB reached both ready query nodes.')
        break
    time.sleep(1)
else:
    raise RuntimeError('Off-pool relay/private LB did not reach both query nodes within deadline.')
PY
""",
        {},
        timeout=240,
    )
    print(f"Published both nodes. HTTP entry: {out['http_url']} (private by default).")
    print(
        "Per-node readiness and off-pool relay/private LB queries to both nodes verified. "
        "External-client load and failover remain separate tests."
    )


def tag_owned_disks(out: dict) -> None:
    state = json.loads(tf("show", "-json"))
    for resource in resources(state["values"]):
        if resource["type"] != "azurerm_linux_virtual_machine":
            continue
        vm = resource["values"]
        identifier = (
            f"/subscriptions/{os.environ['ARM_SUBSCRIPTION_ID']}/resourceGroups/{RG}"
            f"/providers/Microsoft.Compute/disks/{vm['os_disk'][0]['name']}"
        )
        disk = az(
            ["resource", "show", "--ids", identifier, "--query", "{managedBy:managedBy,tags:tags}"]
        )
        if str(disk.get("managedBy", "")).lower() != vm["id"].lower():
            raise RuntimeError("OS disk is not attached to the exact state-owned VM.")
        if any((disk.get("tags") or {}).get(k) not in (None, v) for k, v in vm["tags"].items()):
            raise RuntimeError("OS disk ownership conflicts with project tags.")
        az(
            [
                "rest",
                "--method",
                "patch",
                "--url",
                "https://management.azure.com" + identifier + "?api-version=2024-03-02",
            ],
            body={"tags": {**(disk.get("tags") or {}), **vm["tags"]}},
        )


def request_json(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            try:
                body = json.load(response)
            except ValueError:
                body = {"error": "invalid_json"}
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def failover(out: dict, node: str, base: str, output: Path) -> None:
    if output.is_absolute() or ".." in output.parts:
        raise RuntimeError("Result path must remain inside the project.")
    if base != out["http_url"] and not re.fullmatch(
        r"http://(127\.0\.0\.1|localhost):[0-9]+", base
    ):
        raise RuntimeError("Use the exact Terraform HTTP URL or an explicit localhost tunnel.")
    code, health = request_json(base, "/demo/config")
    if code != 200 or len([n for n in health.get("nodes", []) if n.get("ready")]) < 2:
        raise RuntimeError(
            "Reachable HTTP endpoint with both nodes ready is required before fault injection."
        )
    code, created = request_json(
        base,
        "/demo/runs",
        {
            "scenario_id": "qps_below",
            "duration_seconds": 60,
            "qps": 3,
            "concurrency": 2,
            "limiter_mode": "distributed",
        },
    )
    if code not in (200, 201, 202) or "run_id" not in created:
        raise RuntimeError("Could not start the shared load-core run; no fault injected.")
    vm = out["node_vm_names"][node]
    survivor = "node-2" if node == "node-1" else "node-1"
    samples: list[dict] = []
    started = time.monotonic()
    stopped_at = None
    recovered_at = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        stopping = executor.submit(
            managed_command,
            out,
            vm,
            "docker stop proxysql >/dev/null\n"
            "docker inspect -f '{{.State.Running}}' limiter | grep -qx true\n",
            timeout=120,
        )
        try:
            deadline = started + 240
            while time.monotonic() < deadline:
                stamp = time.monotonic()
                code, response = request_json(
                    base,
                    "/query",
                    {
                        "query_id": "qps_demo",
                        "limiter_mode": "distributed",
                    },
                )
                samples.append(
                    {
                        "after_dispatch_ms": (stamp - started) * 1000,
                        "status": code,
                        "node_id": response.get("node_id"),
                    }
                )
                if stopping.done():
                    stopping.result()
                    if stopped_at is None:
                        stopped_at = time.monotonic()
                    if code == 200 and response.get("node_id") == survivor and recovered_at is None:
                        recovered_at = stamp
                    if time.monotonic() - stopped_at > 15:
                        break
                time.sleep(0.5)
            else:
                raise RuntimeError("Stop/recovery polling exceeded four minutes.")
        finally:
            # Do not race a still-running stop command with the recovery command.
            try:
                stopping.result()
            finally:
                managed_command(out, vm, "docker start proxysql >/dev/null\n", timeout=120)
    for _ in range(90):
        code, health = request_json(base, "/demo/config")
        if code == 200 and any(
            n.get("node_id") == node and n.get("ready") for n in health.get("nodes", [])
        ):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Restored node did not return ready within three minutes.")
    for _ in range(90):
        code, snapshot = request_json(base, f"/demo/runs/{created['run_id']}")
        if code == 200 and snapshot.get("status") != "running":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Shared load-core run did not complete.")
    if recovered_at is None:
        raise RuntimeError(
            "No successful surviving-node request was observed after stop confirmation."
        )
    with urllib.request.urlopen(base.rstrip("/") + "/metrics", timeout=5) as response:
        metrics = response.read(262144).decode()
    failures = sum(sample["status"] == 0 or sample["status"] >= 500 for sample in samples)
    first_unavailable = next(
        (
            sample["after_dispatch_ms"]
            for sample in samples
            if sample["status"] == 0 or sample["status"] >= 500
        ),
        None,
    )
    probe_recovery_ms = None
    if first_unavailable is not None:
        probe_recovery_ms = next(
            (
                sample["after_dispatch_ms"] - first_unavailable
                for sample in samples
                if sample["status"] == 200 and sample["after_dispatch_ms"] > first_unavailable
            ),
            None,
        )
    snapshot["scenario_id"] = "failover"
    snapshot["summary"]["recovery_time_ms"] = probe_recovery_ms
    snapshot["summary"]["transient_failures"] = failures
    snapshot["fault_observation"] = {
        "target": node,
        "survivor": survivor,
        "restored_ready": True,
        "dispatch_to_survivor_ms": (recovered_at - started) * 1000,
        "measurement": "Recovery is first unavailable probe to next valid HTTP 200, or null. "
        "Dispatch-to-survivor includes control-plane delay. Status 0 means no valid HTTP response. "
        "Independent LB probes are not counted in load-core request totals.",
        "samples": samples,
        "metrics_after": metrics,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(
        f"Failover verified: {survivor} served requests; {node} restored ready; "
        f"{failures} failed probes. Result: {output}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "deploy", "destroy", "publish", "failover"))
    parser.add_argument("--action", choices=("stop", "start"))
    parser.add_argument("--node", choices=("node-1", "node-2"), default="node-1")
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/azure-failover.json"))
    args = parser.parse_args()
    context()
    if args.operation == "plan":
        with planned(validation=True) as (_, _, config):
            cost_summary(config)
    elif args.operation == "deploy":
        with planned(validation=False) as (path, plan, config):
            cost_summary(config)
            print("Resources in the approved plan:")
            for resource in resources(plan.get("planned_values", {})):
                if resource["mode"] == "managed":
                    print(
                        f"  {resource['type']}: {resource['values'].get('name', resource['name'])}"
                    )
            confirm(
                "DEPLOY",
                f"Apply project {config['project_id']} and publish this working tree.",
            )
            print("Applying approved plan; resource outputs and sensitive values suppressed.")
            tf("apply", "-input=false", "-no-color", str(path))
        out = outputs()
        tag_owned_disks(out)
        publish(out)
    elif args.operation == "publish":
        out = outputs()
        confirm(
            "DEPLOY",
            "Republish exact project VMs; credential rotation causes a maintenance interruption.",
        )
        publish(out)
    elif args.operation == "destroy":
        out, state = cleanup_context()
        managed = state_scope(out, state=state, require_complete=False, allow_unlabeled_disks=True)
        print("Exact state whitelist (non-taggable children are bounded by owned parent IDs):")
        for resource in managed:
            print(f"  {resource['type']}: {resource['values'].get('name', resource['name'])}")
        with planned(validation=False, destroying=True) as (path, plan, _):
            before = {r["address"] for r in managed}
            changes = {
                r["address"]
                for r in plan.get("resource_changes", [])
                if r["mode"] == "managed" and "delete" in r["change"]["actions"]
            }
            if not changes.issubset(before):
                raise RuntimeError("Destroy plan exceeds the exact checked state whitelist.")
            confirm(
                out["project_id"],
                "Delete ONLY the displayed owned project resources; rg-dev2 is preserved.",
            )
            tag_owned_disks(out)
            state_scope(out, require_complete=False)
            tf("apply", "-input=false", "-no-color", str(path))
            remaining = tf("state", "list").splitlines()
            if any(not line.startswith("data.") for line in remaining):
                raise RuntimeError("Managed state entries remain after cleanup.")
            print("Exact managed state resources destroyed. Shared rg-dev2 preserved.")
    else:
        out = outputs()
        state_scope(out)
        if not args.action:
            failover(out, args.node, args.base_url or out["http_url"], args.output)
        else:
            vm = out["node_vm_names"][args.node]
            managed_command(
                out,
                vm,
                f"docker {args.action} proxysql >/dev/null\n"
                + (
                    "docker inspect -f '{{.State.Running}}' proxysql | grep -qx true\n"
                    if args.action == "start"
                    else "docker inspect -f '{{.State.Running}}' proxysql | grep -qx false\n"
                ),
            )
            print(
                f"{args.node}: ProxySQL {args.action} verified; limiter container was not stopped."
            )


if __name__ == "__main__":
    try:
        os.umask(0o077)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
        (ROOT / ".local").mkdir(exist_ok=True)
        with (ROOT / ".local/azure-operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
    except (RuntimeError, subprocess.CalledProcessError, KeyError, EOFError) as exc:
        print("ERROR: " + safe(str(exc)), file=sys.stderr)
        sys.exit(1)
