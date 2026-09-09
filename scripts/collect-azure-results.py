#!/usr/bin/env python3
"""Collect only exact-state Azure resources and fixed, nonsecret ProxySQL statistics."""

import argparse
import importlib.util
import json
import os
import urllib.request
from pathlib import Path

from ratelimit_demo.config import http_url

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("demo_azure_ops", ROOT / "infra/azure_ops.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Azure operation module is unavailable")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    ops.context()
    outputs = ops.outputs()
    resources = ops.state_scope(outputs)
    subscription = os.environ["ARM_SUBSCRIPTION_ID"]
    status = {"schema_version": 1, "virtual_machines": [], "mysql": [], "load_balancers": []}
    for resource in resources:
        values = resource["values"]
        if resource["type"] == "azurerm_linux_virtual_machine":
            status["virtual_machines"].append(
                ops.az(
                    [
                        "vm",
                        "get-instance-view",
                        "--resource-group",
                        outputs["resource_group_name"],
                        "--name",
                        values["name"],
                        "--subscription",
                        subscription,
                        "--query",
                        "{name:name,statuses:instanceView.statuses[].code}",
                    ]
                )
            )
        elif resource["type"] == "azurerm_mysql_flexible_server":
            status["mysql"].append(
                ops.az(
                    [
                        "mysql",
                        "flexible-server",
                        "show",
                        "--resource-group",
                        outputs["resource_group_name"],
                        "--name",
                        values["name"],
                        "--subscription",
                        subscription,
                        "--query",
                        "{name:name,state:state,publicAccess:network.publicNetworkAccess}",
                    ]
                )
            )
        elif resource["type"] == "azurerm_lb" and (
            resource["name"] == "private" or outputs["public_http_enabled"]
        ):
            metrics = ops.az(
                [
                    "monitor",
                    "metrics",
                    "list",
                    "--resource",
                    values["id"],
                    "--metric",
                    "DipAvailability",
                    "--interval",
                    "PT1M",
                    "--aggregation",
                    "Average",
                    "--subscription",
                    subscription,
                    "--query",
                    "value[].{name:name.value,timeseries:timeseries}",
                ]
            )
            status["load_balancers"].append({"name": values["name"], "probe_metrics": metrics})
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "azure-status.json").write_text(json.dumps(status, indent=2) + "\n")

    script = (
        "set -euo pipefail\ncd /opt/proxysql-demo\n"
        "docker run --rm --pull=never --network container:proxysql --env-file proxy.env "
        "proxysql-demo-app:azure python /app/scripts/proxysql-admin.py --stats-only\n"
    )
    stats = []
    for node, vm in outputs["node_vm_names"].items():
        result = ops.az(
            [
                "vm",
                "run-command",
                "invoke",
                "--resource-group",
                outputs["resource_group_name"],
                "--name",
                vm,
                "--subscription",
                subscription,
                "--command-id",
                "RunShellScript",
                "--scripts",
                script,
                "--query",
                "value[].message",
            ]
        )
        documents = [
            json.loads(line)
            for message in result
            for line in message.splitlines()
            if line.startswith('{"runtime_mysql_servers"')
        ]
        if len(documents) != 1:
            raise RuntimeError(f"No complete ProxySQL statistics returned from {node}")
        stats.append({"node_id": node, **documents[0]})
    (artifacts / "proxysql-stats.jsonl").write_text(
        "".join(json.dumps(document) + "\n" for document in stats)
    )
    base_url = http_url(args.base_url or outputs["http_url"])
    code, health = ops.request_json(base_url, "/demo/config")
    if code != 200:
        raise RuntimeError(
            "Azure resource statistics saved, but HTTP entry is unreachable; "
            "run collection from an authorized VNet-connected host."
        )
    (artifacts / "health.json").write_text(json.dumps(health, indent=2) + "\n")
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as response:
        (artifacts / "metrics.prom").write_bytes(response.read(1_048_576))
    print("Exact-state Azure status, LB probe metrics, and both ProxySQL statistics collected.")


if __name__ == "__main__":
    main()
