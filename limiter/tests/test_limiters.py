import asyncio
import socket

import pytest

from ratelimit_demo.config import ConcurrencyLimit, Limits, QPSLimit
from ratelimit_demo.limiters import Limiters


async def test_explicit_redis_endpoint_failure_is_not_skipped(monkeypatch, namespace):
    from conftest import redis_client

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    monkeypatch.setenv("TEST_REDIS_URL", f"redis://127.0.0.1:{sock.getsockname()[1]}/15")
    fixture = redis_client.__wrapped__(namespace)
    try:
        with pytest.raises(pytest.fail.Exception, match="Explicit TEST_REDIS_URL"):
            await anext(fixture)
    finally:
        await fixture.aclose()
        sock.close()


async def test_local_bucket_independent_without_redis():
    config = Limits(qps_demo=QPSLimit(rate_per_second=0.01, burst=5))
    one = Limiters(None, config)
    two = Limiters(None, config)
    results = await asyncio.gather(*(one.qps("local") for _ in range(12)))
    assert sum(result.allowed for result in results) == 5
    assert (await two.qps("local")).allowed
    assert all(result.retry_after > 0 for result in results if not result.allowed)


async def test_local_concurrency_queue_and_release():
    config = Limits(concurrency_demo=ConcurrencyLimit(max_concurrency=1, queue_timeout_ms=50))
    limiter = Limiters(None, config)
    first = await limiter.acquire("local")
    refused = await limiter.acquire("local")
    assert first.allowed and not refused.allowed
    await limiter.release("local", first.lease_id)
    assert (await limiter.acquire("local")).allowed


async def test_real_redis_two_instance_atomic_qps(redis_client, namespace):
    config = Limits(qps_demo=QPSLimit(rate_per_second=0.01, burst=5))
    instances = [Limiters(redis_client, config, namespace=namespace) for _ in range(2)]
    decisions = await asyncio.gather(*(instances[i % 2].qps("distributed") for i in range(60)))
    assert sum(result.allowed for result in decisions) == 5
    assert all(result.retry_after > 0 for result in decisions if not result.allowed)
    # Local accounting is independent of both the other node and the distributed bucket.
    for limiter in instances:
        assert (await limiter.qps("local")).allowed


async def test_real_redis_server_time_refill(redis_client, namespace):
    config = Limits(qps_demo=QPSLimit(rate_per_second=10, burst=1))
    limiter = Limiters(redis_client, config, namespace=namespace)
    assert (await limiter.qps("distributed")).allowed
    assert not (await limiter.qps("distributed")).allowed
    await asyncio.sleep(0.12)
    assert (await limiter.qps("distributed")).allowed


async def test_real_redis_two_instance_concurrency(redis_client, namespace):
    config = Limits(concurrency_demo=ConcurrencyLimit(max_concurrency=3, queue_timeout_ms=0))
    instances = [Limiters(redis_client, config, namespace=namespace) for _ in range(2)]
    decisions = await asyncio.gather(*(instances[i % 2].acquire("distributed") for i in range(30)))
    accepted = [result for result in decisions if result.allowed]
    assert len(accepted) == 3
    assert sorted(result.active for result in accepted) == [1, 2, 3]
    await instances[1].release("distributed", accepted[0].lease_id)
    assert (await instances[0].acquire("distributed")).allowed


async def test_real_redis_crashed_lease_reclaimed(redis_client, namespace):
    config = Limits(concurrency_demo=ConcurrencyLimit(max_concurrency=1, queue_timeout_ms=0))
    limiter = Limiters(redis_client, config, lease_ttl_ms=80, namespace=namespace)
    assert (await limiter.acquire("distributed")).allowed
    assert not (await limiter.acquire("distributed")).allowed
    await asyncio.sleep(0.1)
    assert (await limiter.acquire("distributed")).allowed


@pytest.mark.parametrize("mode", ["local", "distributed"])
async def test_queue_admits_when_released(redis_client, namespace, mode):
    config = Limits(concurrency_demo=ConcurrencyLimit(max_concurrency=1, queue_timeout_ms=300))
    limiter = Limiters(redis_client, config, namespace=namespace)
    first = await limiter.acquire(mode)
    waiting = asyncio.create_task(limiter.acquire(mode))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await limiter.release(mode, first.lease_id)
    second = await waiting
    assert second.allowed
    await limiter.release(mode, second.lease_id)
