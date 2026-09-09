#!/usr/bin/env python3
"""Measure local LB failover while the shared demo load core runs."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx


async def control(action: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "scripts/docker.sh", "compose", action, "proxysql-1"
    )
    if await process.wait():
        raise RuntimeError(f"Failed to {action} this project's proxysql-1 service")


async def main(base_url: str, output: Path) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        response = await client.post(
            "/demo/runs",
            json={
                "scenario_id": "qps_below",
                "duration_seconds": 20,
                "qps": 3,
                "concurrency": 2,
                "limiter_mode": "distributed",
            },
        )
        response.raise_for_status()
        run_id = response.json()["run_id"]
        await asyncio.sleep(3)
        samples = []
        stopping_at = time.monotonic()
        try:
            await control("stop")
            for _ in range(25):
                started = time.monotonic()
                try:
                    async with httpx.AsyncClient(
                        base_url=base_url,
                        timeout=3,
                        limits=httpx.Limits(max_keepalive_connections=0),
                    ) as probe:
                        result = await probe.post(
                            "/query", json={"query_id": "qps_demo", "limiter_mode": "distributed"}
                        )
                    node = result.json().get("node_id")
                    status = result.status_code
                except (httpx.HTTPError, ValueError) as exc:
                    node, status = None, 0
                    print(f"Failover probe failed: {type(exc).__name__}")
                samples.append(
                    {
                        "after_stop_ms": (started - stopping_at) * 1000,
                        "status": status,
                        "node_id": node,
                    }
                )
                await asyncio.sleep(0.25)
        finally:
            await control("start")

        for _ in range(60):
            health = await client.get("/demo/config")
            health.raise_for_status()
            nodes = health.json()["nodes"]
            if any(node["node_id"] == "node-1" and node["ready"] for node in nodes):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Restored node-1 did not become ready within 60 seconds")

        for _ in range(90):
            result = await client.get(f"/demo/runs/{run_id}")
            result.raise_for_status()
            snapshot = result.json()
            if snapshot["status"] != "running":
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Failover load run did not finish within deadline")
        if snapshot["status"] != "completed":
            raise RuntimeError(f"Failover load run ended with status {snapshot['status']}")
        if not {"node-1", "node-2"}.issubset(snapshot["summary"]["nodes"]):
            raise RuntimeError("Load-core requests did not traverse both LB backend nodes")
        last_failure = max(
            (
                index
                for index, sample in enumerate(samples)
                if sample["status"] == 0 or sample["status"] >= 500
            ),
            default=-1,
        )
        recovered = [
            sample
            for sample in samples[last_failure + 1 :]
            if sample["status"] == 200 and sample["node_id"] == "node-2"
        ]
        if not recovered:
            raise RuntimeError(
                "No sustained recovery to node-2 observed after the final failed probe"
            )
        for _ in range(30):
            response = await client.post("/query", json={"query_id": "qps_demo"})
            if response.status_code == 200 and response.json().get("node_id") == "node-1":
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Restored node-1 did not receive successful LB traffic")
        snapshot["scenario_id"] = "failover"
        snapshot["expected"] = (
            "Surviving node serves requests; stopped node receives traffic after restart."
        )
        snapshot["summary"]["recovery_time_ms"] = recovered[0]["after_stop_ms"]
        snapshot["summary"]["transient_failures"] = sum(
            sample["status"] == 0 or sample["status"] >= 500 for sample in samples
        )
        snapshot["fault_observation"] = {
            "target": "proxysql-1",
            "restored_ready": True,
            "measurement": (
                "Independent LB probes; not included in load-core request counts. "
                "Recovery is the first survivor success after the final failure in this window."
            ),
            "samples": samples,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_text, json.dumps(snapshot, indent=2) + "\n")
        print(
            f"node-2 served traffic, node-1 restored; observed recovery "
            f"{snapshot['summary']['recovery_time_ms']:.0f} ms, "
            f"{snapshot['summary']['transient_failures']} failed probes. Result: {output}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, default=Path("artifacts/failover.json"))
    arguments = parser.parse_args()
    asyncio.run(main(arguments.base_url, arguments.output))
