import asyncio
import json

from ratelimit_demo.models import RunRequest, new_snapshot, utcnow
from ratelimit_demo.store import RunCapacityError, RunStore


def snapshot():
    return new_snapshot(RunRequest(scenario_id="qps_below"), "local")


async def test_global_admission_final_immutability_and_events(redis_client, namespace):
    one = RunStore(redis_client, namespace=namespace)
    two = RunStore(redis_client, namespace=namespace)
    runs = [snapshot() for _ in range(10)]

    async def attempt(index):
        try:
            await (one if index % 2 else two).create(runs[index])
            return runs[index]
        except RunCapacityError:
            return None

    accepted = [run for run in await asyncio.gather(*(attempt(i) for i in range(10))) if run]
    assert len(accepted) == 2
    run = accepted[0]
    assert await two.get(run["run_id"]) == run
    assert await one.publish(run) == 2
    run["status"] = "completed"
    run["finished_at"] = utcnow()
    assert await two.publish(run) == 3
    assert await one.publish({**run, "status": "running"}) == 0
    events = await two.events(run["run_id"], "1-0", block=1)
    assert [event[0] for event in events] == ["2-0", "3-0"]
    assert events[-1][1]["event"] == "complete"
    assert json.loads(events[-1][1]["data"])["status"] == "completed"
    await one.create(snapshot())


async def test_shared_cancel_is_idempotent(redis_client, namespace):
    one = RunStore(redis_client, namespace=namespace)
    two = RunStore(redis_client, namespace=namespace)
    run = snapshot()
    await one.create(run)
    assert await two.cancel(run["run_id"]) == run
    assert await two.cancel(run["run_id"]) == run
    assert await one.cancelled(run["run_id"])
    assert await two.cancel("missing") is None


async def test_expiry_and_worker_loss(redis_client, namespace):
    store = RunStore(redis_client, ttl=1, heartbeat_ttl=1, namespace=namespace)
    run = snapshot()
    await store.create(run)
    await redis_client.delete(store.key(run["run_id"], "worker"))
    failed = await store.get(run["run_id"])
    assert failed["status"] == "failed"
    assert failed["summary"]["errors"] == {"worker_heartbeat_lost": 1}
    assert (await store.events(run["run_id"], "1-0", 1))[-1][1]["event"] == "complete"
    await asyncio.sleep(1.05)
    assert await store.get(run["run_id"]) is None


async def test_heartbeat_expiration_is_explicit(redis_client, namespace):
    store = RunStore(redis_client, namespace=namespace)
    state = {
        "node_id": "node-1",
        "ready": True,
        "proxysql": True,
        "mysql": True,
        "redis": True,
        "limiter_mode": "distributed",
    }
    await store.heartbeat(state)
    assert await store.nodes() == [state]
    await redis_client.delete(namespace + "node:node-1")
    dead = (await store.nodes())[0]
    assert dead["ready"] is False
    assert dead["error"] == "heartbeat_expired"
