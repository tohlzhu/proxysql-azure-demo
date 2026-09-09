import asyncio
import os
import socket
import uuid
from dataclasses import replace

import pytest
import uvicorn
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ratelimit_demo.app import create_app
from ratelimit_demo.config import Limits, Settings


@pytest.fixture
def namespace():
    return "test-" + uuid.uuid4().hex


@pytest.fixture
async def redis_client(namespace):
    explicit_endpoint = "TEST_REDIS_URL" in os.environ
    url = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
    try:
        await client.ping()
    except (RedisError, OSError):
        await client.aclose()
        if explicit_endpoint:
            pytest.fail(
                "Explicit TEST_REDIS_URL is unreachable or authentication failed",
                pytrace=False,
            )
        pytest.skip("Real Redis unavailable; set TEST_REDIS_URL to an isolated test Redis endpoint")
    yield client
    keys = [key async for key in client.scan_iter(match=namespace + "*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


class FakeDatabase:
    def __init__(self):
        self.failure = False
        self.delay = 0.001
        self.queries = 0

    async def query(self, query_id, customer_id):
        if self.failure:
            raise OSError("test database unavailable")
        await asyncio.sleep(self.delay)
        self.queries += 1
        return [{"customer_id": customer_id, "name": "Test customer"}]

    async def health(self):
        return {"proxysql": not self.failure, "mysql": not self.failure}

    async def close(self):
        pass


@pytest.fixture
async def app_pair(redis_client, namespace):
    apps = []
    for node in ("node-1", "node-2"):
        app = create_app(
            Settings(node_id=node, database_tls=False), Limits(), FakeDatabase(), redis_client
        )
        app.state.limiters.namespace = namespace
        app.state.store.prefix = namespace + ":runs:"
        apps.append(app)
    yield apps
    for app in apps:
        tasks = list(app.state.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def live_pair(redis_client, namespace):
    sockets = []
    for _ in range(2):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        sockets.append(sock)
    urls = tuple(f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in sockets)
    servers, tasks, apps = [], [], []
    for index, sock in enumerate(sockets):
        settings = replace(
            Settings(),
            node_id=f"node-{index + 1}",
            target_urls=urls,
            load_url=urls[0],
            database_tls=False,
        )
        app = create_app(settings, Limits(), FakeDatabase(), redis_client)
        app.state.limiters.namespace = namespace
        app.state.store.prefix = namespace + ":runs:"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        servers.append(server)
        tasks.append(task)
        apps.append(app)
    try:
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):
                if any(task.done() for task in tasks):
                    raise RuntimeError("Test HTTP node failed to start")
                await asyncio.sleep(0.02)
        yield urls, apps
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks)
        for sock in sockets:
            sock.close()
