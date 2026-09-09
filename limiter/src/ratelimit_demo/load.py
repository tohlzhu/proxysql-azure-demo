import asyncio
import copy
import math
import time
from collections import Counter

import httpx

from .models import empty_summary, utcnow
from .store import RunStore


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    ordered = sorted(values)
    result = {}
    for label, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        index = (len(ordered) - 1) * quantile
        lower = math.floor(index)
        upper = math.ceil(index)
        result[label] = round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3
        )
    return result


class Measurements:
    def __init__(self):
        self.samples: list[tuple[int, int, float, str, str, int]] = []
        self.active = 0
        self.peak = 0
        self.bucket_peaks: dict[int, int] = {}
        self.failure_started_ms: float | None = None
        self.recovery_time_ms: float | None = None

    def enter(self, second: int) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.bucket_peaks[second] = max(self.bucket_peaks.get(second, 0), self.active)

    def record(
        self,
        second: int,
        code: int,
        latency: float,
        node: str,
        error: str,
        sql: int,
        completed_ms: float | None = None,
    ) -> None:
        self.samples.append((second, code, latency, node, error, sql))
        self.active -= 1
        completed_ms = completed_ms if completed_ms is not None else time.monotonic() * 1000
        if (
            code == 0 or code >= 500 or error == "invalid_response"
        ) and self.failure_started_ms is None:
            self.failure_started_ms = completed_ms
        if (
            code == 200
            and not error
            and self.failure_started_ms is not None
            and self.recovery_time_ms is None
        ):
            self.recovery_time_ms = max(0, completed_ms - self.failure_started_ms)

    def snapshot(self, base: dict) -> dict:
        result = copy.deepcopy(base)
        summary = empty_summary()
        codes = Counter(str(sample[1]) for sample in self.samples)
        total = len(self.samples)
        success = sum(sample[1] == 200 and not sample[4] for sample in self.samples)
        unavailable = sum(
            sample[1] == 0 or sample[1] >= 500 or sample[4] == "invalid_response"
            for sample in self.samples
        )
        summary.update(
            {
                "total": total,
                "success": success,
                "rate_limited": codes["429"],
                "unavailable": unavailable,
                "success_rate": success / total if total else 0,
                "rate_limit_rate": codes["429"] / total if total else 0,
                "peak_concurrency": self.peak,
                "peak_sql_concurrency": max((sample[5] for sample in self.samples), default=0),
                "status_codes": dict(codes),
                "latency_ms": percentiles([sample[2] for sample in self.samples]),
                "nodes": dict(Counter(sample[3] for sample in self.samples if sample[3])),
                "errors": dict(Counter(sample[4] for sample in self.samples if sample[4])),
                "transient_failures": unavailable,
                "recovery_time_ms": self.recovery_time_ms,
            }
        )
        result["summary"] = summary
        result["buckets"] = []
        seconds = sorted({sample[0] for sample in self.samples} | set(self.bucket_peaks))
        for second in seconds:
            samples = [sample for sample in self.samples if sample[0] == second]
            counts = Counter(sample[1] for sample in samples)
            result["buckets"].append(
                {
                    "second": second,
                    "total": len(samples),
                    "success": sum(sample[1] == 200 and not sample[4] for sample in samples),
                    "rate_limited": counts[429],
                    "unavailable": sum(
                        sample[1] == 0 or sample[1] >= 500 or sample[4] == "invalid_response"
                        for sample in samples
                    ),
                    "concurrency": self.bucket_peaks.get(second, 0),
                    "latency_ms": percentiles([sample[2] for sample in samples]),
                    "nodes": dict(Counter(sample[3] for sample in samples if sample[3])),
                }
            )
        return result


class LoadRunner:
    def __init__(
        self,
        store: RunStore,
        targets: tuple[str, ...],
        load_url: str = "http://127.0.0.1:8080",
    ):
        self.store = store
        self.targets = targets
        self.load_url = load_url

    def targets_for(self, scenario_id: str) -> tuple[str, ...]:
        return self.targets if scenario_id == "distributed" else (self.load_url,)

    async def run(self, base: dict) -> None:
        run_id = base["run_id"]
        measures = Measurements()
        tasks: set[asyncio.Task] = set()
        stop = asyncio.Event()
        cancelled = False
        started = time.monotonic()
        params = base["parameters"]
        targets = self.targets_for(base["scenario_id"])
        query_id = (
            "concurrency_demo" if base["scenario_id"].startswith("concurrency") else "qps_demo"
        )

        async def request(index: int, client: httpx.AsyncClient) -> None:
            second = int(time.monotonic() - started)
            measures.enter(second)
            before = time.monotonic()
            code, node, error, sql = 0, "", "request_cancelled", 0
            try:
                response = await client.post(
                    targets[index % len(targets)] + "/query",
                    json={"query_id": query_id, "limiter_mode": base["limiter_mode"]},
                    headers={"Connection": "close"},
                )
                code = response.status_code
                try:
                    data = response.json()
                    node = str(data.get("node_id", ""))
                    sql = int(data.get("sql_concurrency", 0))
                    error = str(data.get("reason", "")) if code != 200 else ""
                    if code != 200 and not error:
                        error = f"http_{code}"
                    if code == 200 and not node:
                        error = "invalid_response"
                except (ValueError, AttributeError, TypeError):
                    error = "invalid_response" if code == 200 else f"http_{code}"
            except httpx.HTTPError:
                error = "transport_error"
            finally:
                measures.record(second, code, (time.monotonic() - before) * 1000, node, error, sql)

        async def report() -> None:
            nonlocal cancelled
            while not stop.is_set():
                await self.store.pulse(run_id)
                if await self.store.cancelled(run_id):
                    cancelled = True
                    stop.set()
                if not await self.store.publish(measures.snapshot(base)):
                    raise RuntimeError("Run was finalized or expired by another node")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass

        reporter = asyncio.create_task(report())
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(7, connect=2),
                limits=httpx.Limits(
                    max_connections=params["concurrency"], max_keepalive_connections=0
                ),
                trust_env=False,
            ) as client:
                count = params["duration_seconds"] * params["qps"]
                next_launch_at = started
                interval = 1 / params["qps"]
                for index in range(count):
                    if reporter.done():
                        reporter.result()
                    if stop.is_set():
                        break
                    remaining = next_launch_at - time.monotonic()
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=remaining)
                        except TimeoutError:
                            pass
                    if stop.is_set():
                        break
                    while len(tasks) >= params["concurrency"]:
                        _, tasks = await asyncio.wait(
                            tasks, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                        )
                        if reporter.done():
                            reporter.result()
                        if (
                            stop.is_set()
                            or time.monotonic() - started >= params["duration_seconds"]
                        ):
                            break
                    if stop.is_set() or time.monotonic() - started >= params["duration_seconds"]:
                        break
                    task = asyncio.create_task(request(index, client))
                    tasks.add(task)
                    # Missed slots are not debt: latency and capacity stalls must not create bursts.
                    next_launch_at = max(next_launch_at, time.monotonic()) + interval
                while tasks and not stop.is_set():
                    _, tasks = await asyncio.wait(
                        tasks, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    if reporter.done():
                        reporter.result()
                if stop.is_set():
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            final = measures.snapshot(base)
            final["status"] = "cancelled" if cancelled else "completed"
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- persist all worker failures
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            final = measures.snapshot(base)
            final["status"] = "failed"
            final["summary"]["errors"][
                "worker_shutdown" if isinstance(exc, asyncio.CancelledError) else "worker_error"
            ] = 1
        finally:
            stop.set()
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        final["finished_at"] = utcnow()
        await self.store.publish(final)
