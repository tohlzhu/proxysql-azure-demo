import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from .config import http_url
from .models import SCENARIOS


def validate_result(
    result: dict,
    limits: dict,
    all_checks: bool = True,
    allow_unavailable: bool = False,
) -> None:
    summary = result["summary"]
    if result["schema_version"] != 1 or result["status"] != "completed":
        raise ValueError("Run did not complete successfully")
    if summary["total"] <= 0 or summary["total"] != sum(summary["status_codes"].values()):
        raise ValueError("Invalid request counters")
    if sum(bucket["total"] for bucket in result["buckets"]) != summary["total"]:
        raise ValueError("Bucket counts do not match summary")
    if (summary["unavailable"] and not allow_unavailable) or any(
        int(code) not in {200, 429}
        and not (allow_unavailable and (int(code) == 0 or int(code) >= 500))
        for code in summary["status_codes"]
    ):
        raise ValueError("Run contained unavailable or unexpected HTTP responses")
    for key, count in (("success_rate", "success"), ("rate_limit_rate", "rate_limited")):
        if abs(summary[key] - summary[count] / summary["total"]) > 1e-9:
            raise ValueError("Invalid fractional rate")
    latency = summary["latency_ms"]
    if not 0 <= latency["p50"] <= latency["p95"] <= latency["p99"]:
        raise ValueError("Invalid latency percentiles")
    if not all_checks:
        return
    scenario = result["scenario_id"]
    if scenario.endswith("_below") and summary["rate_limited"]:
        raise ValueError("Below-limit scenario unexpectedly rejected requests")
    if (scenario.endswith("_above") or scenario == "distributed") and (
        not summary["success"] or not summary["rate_limited"]
    ):
        raise ValueError("Above-limit scenario must demonstrate both successes and rejections")
    if scenario == "distributed" and len(summary["nodes"]) < 2:
        raise ValueError("Distributed scenario did not reach two distinct nodes")
    if (
        scenario in {"qps_below", "qps_above", "distributed"}
        and result["limiter_mode"] == "distributed"
    ):
        duration = result["parameters"]["duration_seconds"]
        config = limits["qps_demo"]
        maximum = config["burst"] + config["rate_per_second"] * duration
        if summary["success"] > maximum:
            raise ValueError("Global QPS quota exceeded (possible doubled per-node quota)")
    if (
        scenario.startswith("concurrency")
        and result["limiter_mode"] == "distributed"
        and summary["peak_sql_concurrency"] > limits["concurrency_demo"]["max_concurrency"]
    ):
        raise ValueError("Global SQL concurrency exceeded")


def save_results(results: list[dict], output: Path, all_scenarios: bool) -> None:
    if all_scenarios and output.suffix.lower() == ".json":
        output.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": 1, "runs": results}
    else:
        data = results[-1]
        if all_scenarios:
            output.mkdir(parents=True, exist_ok=True)
            output = output / f"{data['scenario_id']}.json"
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


async def execute(args: argparse.Namespace) -> list[dict]:
    async with httpx.AsyncClient(
        base_url=http_url(args.base_url), timeout=10, trust_env=False
    ) as client:
        response = await client.get("/demo/config")
        response.raise_for_status()
        config = response.json()
        scenarios = [key for key in SCENARIOS if key != "failover"] if args.all else [args.scenario]
        results = []
        for scenario in scenarios:
            params = dict(SCENARIOS[scenario]["defaults"])
            params["duration_seconds"] = args.duration
            if args.qps is not None:
                params["qps"] = args.qps
            if args.concurrency is not None:
                params["concurrency"] = args.concurrency
            response = await client.post(
                "/demo/runs",
                json={
                    **params,
                    "scenario_id": scenario,
                    "limiter_mode": args.mode,
                },
            )
            response.raise_for_status()
            result = response.json()
            run_id = result["run_id"]
            deadline = time.monotonic() + args.duration + 25
            try:
                while result["status"] == "running":
                    if time.monotonic() > deadline:
                        raise TimeoutError("Run exceeded its bounded completion time")
                    await asyncio.sleep(0.25)
                    response = await client.get("/demo/runs/" + run_id)
                    response.raise_for_status()
                    result = response.json()
            except BaseException:
                try:
                    await client.post(f"/demo/runs/{run_id}/cancel")
                except httpx.HTTPError:
                    pass
                raise
            results.append(result)
            output = Path(args.output or ("artifacts" if args.all else "artifacts/result.json"))
            save_results(results, output, args.all)
            validate_result(
                result,
                config["limits"],
                all_checks=True,
                allow_unavailable=getattr(args, "allow_unavailable", False),
            )
            print(
                f"{scenario}: {result['status']}; total={result['summary']['total']} "
                f"success={result['summary']['success']} limited={result['summary']['rate_limited']} "
                f"unavailable={result['summary']['unavailable']}"
            )
            if args.all:
                await asyncio.sleep(1)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run bounded SQL scenarios using the same API as the web console"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="qps_below")
    parser.add_argument("--duration", type=int, choices=range(1, 61), default=10)
    parser.add_argument("--mode", choices=["local", "distributed"], default="distributed")
    parser.add_argument("--qps", type=int, choices=range(1, 101))
    parser.add_argument("--concurrency", type=int, choices=range(1, 33))
    parser.add_argument(
        "--output",
        help="JSON file or --all directory; a --all JSON file contains schema_version and runs",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run and assert all five non-failover scenarios",
    )
    parser.add_argument("--assert-results", action="store_true")
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Retain and measure transport/5xx failures for externally controlled fault tests",
    )
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except (httpx.HTTPError, ValueError, KeyError, TimeoutError, OSError) as exc:
        # HTTP exception strings may contain a caller-supplied credential-bearing URL.
        detail = str(exc) if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}; check /readyz and bounded parameters"
        print(f"Demo failed: {detail}; inspect saved results.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
