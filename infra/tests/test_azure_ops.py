"""Offline tests exercise plan, publication, and scope guardrails without Azure writes."""

import base64
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "azure_ops", Path(__file__).resolve().parents[1] / "azure_ops.py"
)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def resource(kind="azurerm_mysql_flexible_server", **values):
    return {
        "mode": "managed",
        "type": kind,
        "name": "example",
        "values": {
            "tags": {"owner": "agent", "project": "demo", "autodelete": "true", "task": "test"},
            **values,
        },
    }


def plan():
    server = resource(public_network_access_enabled=False)
    return {
        "resource_changes": [
            {
                "mode": "managed",
                "type": server["type"],
                "change": {
                    "actions": ["create"],
                    "after": server["values"],
                    "after_unknown": {"delegated_subnet_id": True, "private_dns_zone_id": True},
                },
            }
        ],
        "planned_values": {"root_module": {"resources": [server]}},
        "output_changes": {"http_url": {"after_sensitive": False}},
    }


def test_private_plan_allowed(capsys):
    assert ops.inspect_plan(plan(), "demo") == {"add": 1, "change": 0, "destroy": 0}
    assert "1 add, 0 change, 0 destroy" in capsys.readouterr().out


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"]])
def test_deletion_and_replacement_rejected(actions):
    value = plan()
    value["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(RuntimeError, match="deletion"):
        ops.inspect_plan(value, "demo")


def test_shared_resource_group_never_managed():
    value = plan()
    value["resource_changes"][0]["type"] = "azurerm_resource_group"
    with pytest.raises(RuntimeError, match="group"):
        ops.inspect_plan(value, "demo", destroying=True)


def test_public_database_rejected():
    value = plan()
    value["planned_values"]["root_module"]["resources"][0]["values"][
        "public_network_access_enabled"
    ] = True
    with pytest.raises(RuntimeError, match="Public database"):
        ops.inspect_plan(value, "demo")


def test_missing_private_reference_rejected():
    value = plan()
    value["resource_changes"][0]["change"]["after_unknown"] = {}
    with pytest.raises(RuntimeError, match="private network"):
        ops.inspect_plan(value, "demo")


@pytest.mark.parametrize(
    "name,sensitive", [("http_url", True), ("admin_password", False), ("tenant_id", False)]
)
def test_secret_and_identifier_outputs_rejected(name, sensitive):
    value = plan()
    value["output_changes"] = {name: {"after_sensitive": sensitive}}
    with pytest.raises(RuntimeError, match="output"):
        ops.inspect_plan(value, "demo")


def test_wrong_project_rejected():
    with pytest.raises(RuntimeError, match="ownership"):
        ops.inspect_plan(plan(), "not-demo")


def test_archive_is_bounded_and_contains_dirty_deploy_contract():
    encoded = ops.source_archive()
    assert len(encoded) <= 2_000_000
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as archive:
        names = archive.getnames()
        assert "limiter/Dockerfile" in names
        assert "scripts/init-db.py" in names
        assert "scripts/render-proxysql.py" in names
        assert "infra/templates/compose-node.yaml.tpl" in names
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)
        assert not any(name.endswith((".env", ".pem", ".tfstate", ".pyc")) for name in names)


def test_in_memory_key_is_valid_ssh_rsa():
    key = ops.ephemeral_public_key()
    result = subprocess.run(
        ["ssh-keygen", "-l", "-f", "/dev/stdin"], input=key, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert "2048" in result.stdout and "(RSA)" in result.stdout


@pytest.mark.parametrize("script", [ops.UPLOAD, ops.PUBLISH])
def test_embedded_remote_shell_parses(script):
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_managed_command_cannot_target_other_vm():
    out = {"node_vm_names": {"node-1": "owned-one"}, "redis_vm_name": "owned-redis"}
    with pytest.raises(RuntimeError, match="exact Terraform"):
        ops.managed_command(out, "other-vm", "false")


def test_redaction_removes_credentials_and_identifiers(monkeypatch):
    monkeypatch.setattr(ops, "KNOWN_SECRETS", ["not-a-real-password"])
    assert ops.safe("not-a-real-password 00000000-0000-0000-0000-000000000000") == (
        "[REDACTED] [REDACTED-ID]"
    )


def test_zone_restrictions_are_removed(monkeypatch, capsys):
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "test")
    responses = [
        [
            {
                "name": "Standard_B2ls_v2",
                "zones": ["1", "2", "3"],
                "restrictions": [{"type": "Zone", "restrictionInfo": {"zones": ["2"]}}],
            }
        ],
        [
            {"namespace": name, "state": "Registered"}
            for name in ("Microsoft.Network", "Microsoft.Compute", "Microsoft.DBforMySQL")
        ],
    ]
    monkeypatch.setattr(ops, "az", lambda *args: responses.pop(0))
    ops.select_zones(
        {
            "node_vm_size": "Standard_B2ls_v2",
            "redis_vm_size": "Standard_B2ls_v2",
            "node_zones": [],
            "observability_enabled": False,
        }
    )
    assert ops.os.environ["TF_VAR_node_zones"] == '["1", "3"]'
    assert "zones 1, 3" in capsys.readouterr().out


def test_location_restriction_not_misrepresented_as_zone_downgrade(monkeypatch):
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "test")
    monkeypatch.setattr(
        ops,
        "az",
        lambda *args: [
            {"name": "Standard_B1ms", "zones": ["1", "2"], "restrictions": [{"type": "Location"}]}
        ],
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        ops.select_zones(
            {
                "node_vm_size": "Standard_B1ms",
                "redis_vm_size": "Standard_B1ms",
                "node_zones": [],
                "observability_enabled": False,
            }
        )


def test_regional_downgrade_when_fewer_than_two_zones(monkeypatch, capsys):
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "test")
    replies = [
        [{"name": "Standard_B2ls_v2", "zones": ["1"], "restrictions": []}],
        [
            {"namespace": name, "state": "Registered"}
            for name in ("Microsoft.Network", "Microsoft.Compute", "Microsoft.DBforMySQL")
        ],
    ]
    monkeypatch.setattr(ops, "az", lambda *args: replies.pop(0))
    ops.select_zones(
        {
            "node_vm_size": "Standard_B2ls_v2",
            "redis_vm_size": "Standard_B2ls_v2",
            "node_zones": [],
            "observability_enabled": False,
        }
    )
    assert ops.os.environ["TF_VAR_node_zones"] == "[]"
    assert "AUTOMATIC DOWNGRADE" in capsys.readouterr().out


@pytest.mark.parametrize("capture_output", [False, True])
def test_named_protected_parameter_is_environment_not_literal_script(monkeypatch, capture_output):
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "validation-subscription")
    requests = []

    def fake_az(args, *, body=None):
        requests.append((args, body))
        if "get" in args:
            return {
                "state": "Succeeded",
                "view": {
                    "executionState": "Succeeded",
                    "exitCode": 0,
                    "output": "RESULT=fixture\n",
                },
            }
        return {}

    monkeypatch.setattr(ops, "az", fake_az)
    out = {
        "node_vm_names": {"node-1": "owned-one"},
        "redis_vm_name": "owned-redis",
        "project_id": "demo",
    }
    output = ops.managed_command(
        out,
        "owned-one",
        "true\n",
        {"secret": "test-only-value"},
        capture_output=capture_output,
    )
    assert output == ("RESULT=fixture\n" if capture_output else None)
    body = requests[0][1]
    assert "test-only-value" not in body["properties"]["source"]["script"]
    assert 'set -- "$payload"' in body["properties"]["source"]["script"]
    protected = body["properties"]["protectedParameters"]
    assert protected[0]["name"] == "payload"
    assert "delete" in requests[-1][0]


def test_publishing_retains_restricted_proxy_environment_for_statistics():
    assert "trap 'rm -f admin.env' EXIT" in ops.PUBLISH
    assert "rm -f admin.env proxy.env" not in ops.PUBLISH


def test_partial_apply_cleanup_does_not_depend_on_outputs(monkeypatch):
    state = {"values": {"root_module": {"resources": [resource("azurerm_virtual_network")]}}}
    monkeypatch.setattr(ops, "tf", lambda *args: ops.json.dumps(state))
    out, recovered = ops.cleanup_context()
    assert out == {"project_id": "demo"}
    assert recovered == state


def test_scope_rejects_tampered_vm_outputs(monkeypatch):
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "validation-subscription")
    prefix = "/subscriptions/validation-subscription/resourceGroups/rg-dev2/"
    entry = resource(
        "azurerm_virtual_network",
        id=prefix + "providers/Microsoft.Network/virtualNetworks/demo",
        name="demo",
    )
    state = {"values": {"root_module": {"resources": [entry]}}}
    monkeypatch.setattr(ops, "az", lambda *args: {"name": "demo", "tags": entry["values"]["tags"]})
    out = {"project_id": "demo", "node_vm_names": {"node-1": "other"}, "redis_vm_name": "other2"}
    with pytest.raises(RuntimeError, match="exact state-owned"):
        ops.state_scope(out, state=state)
    assert ops.state_scope(out, state=state, require_complete=False) == [entry]


def test_destroy_cannot_create_resources():
    with pytest.raises(RuntimeError, match="must not create"):
        ops.inspect_plan(plan(), "demo", destroying=True)


def test_transport_failure_does_not_fabricate_http_503(monkeypatch):
    def offline(*args, **kwargs):
        raise ops.urllib.error.URLError("test transport unavailable")

    monkeypatch.setattr(ops.urllib.request, "urlopen", offline)
    assert ops.request_json("http://127.0.0.1:1", "/query", {}) == (0, {})


def test_http_failure_keeps_actual_status(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ops.urllib.error.HTTPError("http://localhost", 503, "Unavailable", {}, None)

    monkeypatch.setattr(ops.urllib.request, "urlopen", unavailable)
    assert ops.request_json("http://127.0.0.1:1", "/query", {}) == (503, {})


def test_publish_separates_browser_load_and_distributed_urls(monkeypatch):
    monkeypatch.setattr(ops, "state_scope", lambda *args, **kwargs: [])
    state = {
        "values": {
            "root_module": {"resources": [resource(administrator_password="validation-only-admin")]}
        }
    }
    monkeypatch.setattr(ops, "tf", lambda *args: ops.json.dumps(state))
    for key in (
        "MYSQL_ADMIN_PASSWORD",
        "MYSQL_APP_PASSWORD",
        "MYSQL_MONITOR_PASSWORD",
        "REDIS_PASSWORD",
        "PROXYSQL_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    calls = []
    monkeypatch.setattr(
        ops,
        "managed_command",
        lambda out, vm, script, payload, **kwargs: calls.append((script, payload)),
    )
    out = {
        "node_vm_names": {"node-1": "demo-node-1", "node-2": "demo-node-2"},
        "redis_vm_name": "demo-redis",
        "node_private_ips": {"node-1": "10.42.1.11", "node-2": "10.42.1.12"},
        "redis_private_ip": "10.42.3.10",
        "mysql_host": "example.mysql.database.azure.com",
        "mysql_admin_user": "demoadmin",
        "http_url": "http://203.0.113.10:8080",
        "private_http_url": "http://10.42.1.10:8080",
    }
    ops.publish(out)
    runtimes = [
        payload["runtime"]
        for script, payload in calls
        if script == ops.PUBLISH and payload["role"] != "redis"
    ]
    assert len(runtimes) == 2
    for runtime in runtimes:
        assert runtime["LIMITER_PUBLIC_URL"] == "http://203.0.113.10:8080"
        assert runtime["LIMITER_LOAD_URL"] == "http://10.42.3.10:8081"
        assert runtime["LIMITER_TARGET_URLS"] == "http://10.42.1.11:8080,http://10.42.1.12:8080"
    redis_payload = next(
        payload for script, payload in calls if script == ops.PUBLISH and payload["role"] == "redis"
    )
    assert "server internal_lb 10.42.1.10:8080" in redis_payload["relay_config"]
    assert "203.0.113.10" not in redis_payload["relay_config"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://8.8.8.8:8080",
        "http://127.0.0.1:8080",
        "http://10.42.1.10:8081",
        "https://10.42.1.10:8080",
        "http://10.42.1.10:8080/query",
        "http://other.example:8080",
    ],
)
def test_load_relay_rejects_non_internal_lb_targets(endpoint):
    with pytest.raises(RuntimeError, match="Relay"):
        ops.relay_config(endpoint)
