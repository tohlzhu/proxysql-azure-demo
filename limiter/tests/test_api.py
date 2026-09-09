import asyncio
import json

import httpx
import pytest


async def test_query_validation_and_config(app_pair):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_pair[0]), base_url="http://test"
    ) as client:
        for body in (
            {"query_id": "arbitrary"},
            {"query_id": "qps_demo", "sql": "SELECT 1"},
            {"query_id": "qps_demo", "customer_id": "1 OR 1=1"},
            {"query_id": "qps_demo", "customer_id": -1},
            {"query_id": "qps_demo", "limiter_mode": "bad"},
        ):
            assert (await client.post("/query", json=body)).status_code == 422
        config = (await client.get("/demo/config")).json()
        assert config["bounds"]["concurrency"] == {"min": 1, "max": 32}
        assert config["limits"]["qps_demo"] == {"rate_per_second": 10, "burst": 5}
        assert next(s for s in config["scenarios"] if s["id"] == "failover")["enabled"] is False
        assert (await client.get("/readyz")).status_code == 200
        app_pair[0].state.database.failure = True
        state = await client.get("/readyz")
        assert state.status_code == 503
        assert state.json()["proxysql"] is False


async def test_query_qps_rejections_and_metrics(app_pair):
    from ratelimit_demo.config import Limits, QPSLimit

    app_pair[0].state.limiters.limits = Limits(qps_demo=QPSLimit(rate_per_second=0.01, burst=5))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_pair[0]), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(client.post("/query", json={"query_id": "qps_demo"}) for _ in range(20))
        )
        assert sum(response.status_code == 200 for response in responses) == 5
        rejected = [response for response in responses if response.status_code == 429]
        assert len(rejected) == 15
        assert all(int(response.headers["retry-after"]) >= 1 for response in rejected)
        assert all(response.json()["reason"] == "qps_limit_exceeded" for response in rejected)
        metrics = (await client.get("/metrics")).text
        assert "limiter_requests_total" in metrics
        assert "limiter_rejections_total" in metrics
        assert "limiter_sql_active" in metrics
        assert "limiter_queue_seconds" in metrics


@pytest.mark.parametrize("mode", ["local", "distributed"])
async def test_database_error_releases_lease(app_pair, mode):
    app = app_pair[0]
    app.state.database.failure = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post("/query", json={"query_id": "concurrency_demo", "limiter_mode": mode})
                for _ in range(3)
            )
        )
        assert all(response.status_code == 503 for response in responses)
        app.state.database.failure = False
        good = await client.post(
            "/query", json={"query_id": "concurrency_demo", "limiter_mode": mode}
        )
        assert good.status_code == 200
        assert good.json()["sql_concurrency"] == 1
    assert not app.state.limiters.leases


@pytest.mark.parametrize("mode", ["local", "distributed"])
async def test_request_cancellation_releases_lease(app_pair, mode):
    app = app_pair[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(
                "/query",
                json={
                    "query_id": "concurrency_demo",
                    "limiter_mode": mode,
                },
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
        limiter = app.state.limiters
        if mode == "local":
            assert len(limiter.leases) == 0
        else:
            assert (
                await limiter.redis.zcard(limiter.namespace + ":distributed:concurrency_demo") == 0
            )


async def test_run_boundaries_disabled_failover_and_missing_ids(app_pair):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_pair[0]), base_url="http://test"
    ) as client:
        for update in (
            {"duration_seconds": 0},
            {"duration_seconds": 61},
            {"qps": 0},
            {"qps": 101},
            {"concurrency": 0},
            {"concurrency": 33},
            {"scenario_id": "nope"},
            {"url": "http://example.com"},
        ):
            response = await client.post("/demo/runs", json={"scenario_id": "qps_below", **update})
            assert response.status_code == 422
        assert (
            await client.post("/demo/runs", json={"scenario_id": "failover"})
        ).status_code == 403
        assert (await client.get("/demo/runs/not-valid")).status_code == 404
        assert (await client.post("/demo/runs/" + "0" * 32 + "/cancel")).status_code == 404


async def test_live_two_node_run_sse_reconnect_cancel_and_counts(live_pair):
    urls, _apps = live_pair
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            urls[0] + "/demo/runs",
            json={
                "scenario_id": "distributed",
                "duration_seconds": 2,
                "qps": 40,
                "concurrency": 16,
                "limiter_mode": "distributed",
            },
        )
        assert response.status_code == 202
        run = response.json()
        run_id = run["run_id"]
        assert (await client.get(urls[1] + f"/demo/runs/{run_id}")).status_code == 200
        async with asyncio.timeout(10):
            while run["status"] == "running":
                await asyncio.sleep(0.1)
                run = (await client.get(urls[1] + f"/demo/runs/{run_id}")).json()
        assert run["status"] == "completed"
        summary = run["summary"]
        assert 0 < summary["total"] <= 80
        assert sum(app.state.database.queries for app in _apps) == summary["success"]
        assert summary["success"] <= 25
        assert summary["rate_limited"] > 0
        assert summary["unavailable"] == 0
        assert set(summary["nodes"]) == {"node-1", "node-2"}
        assert sum(summary["status_codes"].values()) == summary["total"]
        assert sum(bucket["total"] for bucket in run["buckets"]) == summary["total"]
        text = (await client.get(urls[1] + f"/demo/runs/{run_id}/events")).text
        events = [block for block in text.strip().split("\n\n") if block.startswith("id:")]
        ids = [int(block.splitlines()[0].split(": ")[1]) for block in events]
        assert ids == sorted(set(ids))
        assert "event: complete" in events[-1]
        assert json.loads(events[-1].split("data: ", 1)[1]) == run
        resumed = (
            await client.get(
                urls[0] + f"/demo/runs/{run_id}/events",
                headers={
                    "Last-Event-ID": str(ids[-2]),
                },
            )
        ).text
        assert resumed.count("event: complete") == 1
        assert resumed.count("id: ") == 1
        assert (
            await client.get(
                urls[0] + f"/demo/runs/{run_id}/events",
                headers={
                    "Last-Event-ID": "999999",
                },
            )
        ).status_code == 400
        assert (
            await client.get(
                urls[0] + f"/demo/runs/{run_id}/events",
                headers={
                    "Last-Event-ID": "bad",
                },
            )
        ).status_code == 400
        # Cancellation is shared, independent of which node owns the worker.
        run = (
            await client.post(
                urls[0] + "/demo/runs",
                json={
                    "scenario_id": "concurrency_above",
                    "duration_seconds": 10,
                    "qps": 20,
                    "concurrency": 12,
                    "limiter_mode": "local",
                },
            )
        ).json()
        await asyncio.sleep(0.2)
        for _ in range(2):
            assert (
                await client.post(urls[1] + f"/demo/runs/{run['run_id']}/cancel")
            ).status_code == 200
        async with asyncio.timeout(5):
            while run["status"] == "running":
                await asyncio.sleep(0.1)
                run = (await client.get(urls[1] + f"/demo/runs/{run['run_id']}")).json()
        assert run["status"] == "cancelled"
        assert run["finished_at"]


async def test_worker_failure_surfaces_failed(app_pair):
    from ratelimit_demo.models import RunRequest, new_snapshot

    app = app_pair[0]
    run = new_snapshot(RunRequest(scenario_id="qps_below"), "local")
    await app.state.store.create(run)
    await app.state.store.redis.delete(app.state.store.key(run["run_id"], "worker"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_pair[1]), base_url="http://test"
    ) as client:
        result = (await client.get(f"/demo/runs/{run['run_id']}")).json()
        assert result["status"] == "failed"
        assert result["summary"]["errors"]["worker_heartbeat_lost"] == 1


async def test_query_hard_timeout_releases_lease(redis_client, namespace):
    from conftest import FakeDatabase

    from ratelimit_demo.app import create_app
    from ratelimit_demo.config import Limits, Settings

    app = create_app(Settings(query_timeout=0.05), Limits(), FakeDatabase(), redis_client)
    app.state.limiters.namespace = namespace
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/query", json={"query_id": "concurrency_demo"})
        assert result.status_code == 503
        assert result.json()["reason"] == "database_unavailable"
    assert await redis_client.zcard(namespace + ":distributed:concurrency_demo") == 0


async def test_redis_unavailable_is_503():
    import socket

    from conftest import FakeDatabase
    from redis.asyncio import Redis

    from ratelimit_demo.app import create_app
    from ratelimit_demo.config import Limits, Settings

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # Bound, non-listening socket reserves a refused port without affecting any real Redis.
    cache = Redis.from_url(
        f"redis://127.0.0.1:{port}/0",
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
        decode_responses=True,
    )
    try:
        app = create_app(Settings(), Limits(), FakeDatabase(), cache)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post("/query", json={"query_id": "qps_demo"})
            assert result.status_code == 503
            assert result.json()["reason"] == "redis_unavailable"
            result = await client.post("/demo/runs", json={"scenario_id": "qps_below"})
            assert result.status_code == 503
            config = (await client.get("/demo/config")).json()
            assert config["nodes"][0]["redis"] is False
            assert config["nodes"][0]["error"] == "redis_unavailable"
    finally:
        await cache.aclose()
        sock.close()


@pytest.mark.parametrize("mode", ["local", "distributed"])
async def test_cli_all_uses_live_shared_core(live_pair, namespace, mode):
    import argparse
    import shutil
    from pathlib import Path

    from ratelimit_demo.cli import execute

    urls, _apps = live_pair
    output = Path(__file__).resolve().parent / (".results-" + namespace)
    args = argparse.Namespace(
        base_url=urls[0],
        all=True,
        scenario="qps_below",
        duration=2,
        mode=mode,
        qps=None,
        concurrency=None,
        output=str(output),
        assert_results=True,
    )
    try:
        results = await execute(args)
        assert len(results) == 5
        for result in results:
            saved = json.loads((output / (result["scenario_id"] + ".json")).read_text())
            assert saved == result
            assert saved["schema_version"] == 1
            if result["scenario_id"] != "distributed":
                assert set(result["summary"]["nodes"]) == {"node-1"}
    finally:
        if output.exists():
            shutil.rmtree(output)


async def test_worker_stops_if_another_node_finalizes_run(live_pair):
    from ratelimit_demo.models import utcnow

    urls, apps = live_pair
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        run = (
            await client.post(
                urls[0] + "/demo/runs",
                json={
                    "scenario_id": "qps_below",
                    "duration_seconds": 10,
                },
            )
        ).json()
        await asyncio.sleep(0.1)
        run["status"] = "failed"
        run["finished_at"] = utcnow()
        run["summary"]["errors"]["worker_heartbeat_lost"] = 1
        assert await apps[1].state.store.publish(run)
        async with asyncio.timeout(2):
            while run["run_id"] in apps[0].state.workers:
                await asyncio.sleep(0.05)
        result = (await client.get(urls[1] + f"/demo/runs/{run['run_id']}")).json()
        assert result["status"] == "failed"
        assert result["summary"]["errors"]["worker_heartbeat_lost"] == 1


@pytest.mark.parametrize("mode", ["local", "distributed"])
async def test_below_qps_does_not_catch_up_after_a_latency_stall(live_pair, mode):
    import time
    from itertools import pairwise

    urls, apps = live_pair
    database = apps[0].state.database
    original_query = database.query
    arrivals = []

    async def delayed_first_query(query_id, customer_id):
        arrivals.append(time.monotonic())
        if len(arrivals) == 1:
            await asyncio.sleep(1.4)
        return await original_query(query_id, customer_id)

    database.query = delayed_first_query
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            urls[0] + "/demo/runs",
            json={
                "scenario_id": "qps_below",
                "duration_seconds": 3,
                "qps": 5,
                "concurrency": 1,
                "limiter_mode": mode,
            },
        )
        assert response.status_code == 202
        result = response.json()
        async with asyncio.timeout(8):
            while result["status"] == "running":
                await asyncio.sleep(0.1)
                result = (await client.get(urls[1] + f"/demo/runs/{result['run_id']}")).json()
    assert result["status"] == "completed"
    assert result["summary"]["unavailable"] == 0
    assert result["summary"]["rate_limited"] == 0
    assert result["summary"]["success"] >= 5
    assert all(later - earlier >= 0.18 for earlier, later in pairwise(arrivals))
