#!/usr/bin/env python3
"""Bounded verification executed inside the deployed private VNet."""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    message = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(message, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (urllib.error.URLError, TimeoutError):
        return 0, {}


def suites(base: str, directory: Path) -> None:
    for mode in ("distributed", "local"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "ratelimit_demo.cli",
                "--base-url",
                base,
                "--all",
                "--duration",
                "6",
                "--mode",
                mode,
                "--output",
                str(directory / f"{mode}.json"),
            ],
            check=True,
        )
    code, health = request(base, "/demo/config")
    if code != 200 or {n["node_id"] for n in health["nodes"] if n["ready"]} != {
        "node-1",
        "node-2",
    }:
        raise RuntimeError("Both Azure backend nodes must be ready")
    (directory / "health.json").write_text(json.dumps(health, indent=2) + "\n")
    print("Both limiter modes passed all five scenarios through the Azure deployment.")


def web(base: str, directory: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.locator("#scenario").select_option("qps_above")
        page.locator("#duration").fill("4")
        page.locator("#start-run").click()
        page.wait_for_function(
            "() => Number(document.querySelector('#success-count').textContent) > 0 "
            "&& Number(document.querySelector('#rejected-count').textContent) > 0",
            timeout=30000,
        )
        page.reload()
        page.wait_for_function(
            "() => document.querySelector('#run-status').textContent.includes('completed')",
            timeout=30000,
        )
        with page.expect_download() as download:
            page.locator("#download-run").click()
        result = json.loads(download.value.path().read_text())
        if not (
            result["schema_version"] == 1
            and result["status"] == "completed"
            and result["summary"]["success"] > 0
            and result["summary"]["rate_limited"] > 0
            and result["summary"]["unavailable"] == 0
            and not errors
        ):
            raise RuntimeError(
                "Azure browser smoke did not produce valid success/rejection results"
            )
        (directory / "web.json").write_text(json.dumps(result, indent=2) + "\n")
        browser.close()
    print("Azure Chromium smoke passed: start, success/rejection, refresh recovery, JSON download.")


def fault(base: str, directory: Path) -> None:
    code, health = request(base, "/demo/config")
    if code != 200 or len([n for n in health["nodes"] if n["ready"]]) != 2:
        raise RuntimeError("Both nodes must be ready before observing fault injection")
    limits = health["limits"]
    snapshots = []
    current = None
    fault_run = None
    seen_down = False
    seen_survivor = False
    restored_ready = False
    restored_traffic = False
    samples = []
    start = time.monotonic()
    deadline = start + 360
    while time.monotonic() < deadline:
        if current is not None:
            code, current = request(base, f"/demo/runs/{current['run_id']}")
            if code != 200:
                raise RuntimeError("Shared run state became unavailable during ProxySQL failure")
            if current["status"] != "running":
                snapshots.append(current)
                current = None
        if current is None and not restored_traffic:
            code, current = request(
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
            if code not in (200, 201, 202):
                raise RuntimeError("Failed to start the bounded shared load-core run")
            (directory / "fault-ready").write_text(current["run_id"])
        before = time.monotonic()
        code, query = request(
            base, "/query", {"query_id": "qps_demo", "limiter_mode": "distributed"}
        )
        samples.append(
            {"elapsed_ms": (before - start) * 1000, "status": code, "node_id": query.get("node_id")}
        )
        health_code, health = request(base, "/demo/config")
        if health_code == 200:
            nodes = {n["node_id"]: n for n in health["nodes"]}
            if "node-1" in nodes and not nodes["node-1"]["ready"]:
                seen_down = True
                if current:
                    fault_run = current["run_id"]
            if seen_down and nodes.get("node-1", {}).get("ready"):
                restored_ready = True
        if seen_down and code == 200 and query.get("node_id") == "node-2":
            seen_survivor = True
        if restored_ready and code == 200 and query.get("node_id") == "node-1":
            restored_traffic = True
        if restored_traffic and current is None:
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("Fault observer exceeded six minutes without verified restoration")
    if not (seen_down and seen_survivor and restored_traffic and fault_run):
        raise RuntimeError("Did not observe node down, survivor traffic, and restored-node traffic")
    result = next((item for item in snapshots if item["run_id"] == fault_run), None)
    if result is None or result["status"] != "completed":
        raise RuntimeError("The shared load-core run spanning the outage did not complete")
    from ratelimit_demo.cli import validate_result

    validate_result(result, limits, all_checks=False, allow_unavailable=True)
    if not {"node-1", "node-2"}.issubset(result["summary"]["nodes"]):
        raise RuntimeError("The shared load core did not reach both Azure LB backends")
    failures = [s for s in samples if s["status"] == 0 or s["status"] >= 500]
    last_failure = max((s["elapsed_ms"] for s in failures), default=None)
    stable_recovery_ms = None
    if last_failure is not None:
        recovered = next(
            (s for s in samples if s["elapsed_ms"] > last_failure and s["status"] == 200),
            None,
        )
        if recovered is not None:
            stable_recovery_ms = recovered["elapsed_ms"] - failures[0]["elapsed_ms"]
    result["scenario_id"] = "failover"
    result["fault_observation"] = {
        "target": "node-1",
        "survivor": "node-2",
        "down_observed": seen_down,
        "survivor_traffic": seen_survivor,
        "restored_ready": restored_ready,
        "restored_traffic": restored_traffic,
        "failed_probes": len(failures),
        "stable_recovery_ms": stable_recovery_ms,
        "measurement": (
            "Independent probes; recovery spans first failure to success after last failure."
        ),
        "samples": samples,
    }
    (directory / "failover.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"Azure fault verified: node-2 served traffic, node-1 restored; "
        f"{len(failures)} failed probes; recovery_ms={stable_recovery_ms}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("suites", "web", "fault"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--directory", type=Path, default=Path("/results"))
    options = parser.parse_args()
    options.directory.mkdir(parents=True, exist_ok=True)
    {"suites": suites, "web": web, "fault": fault}[options.phase](
        options.base_url, options.directory
    )
