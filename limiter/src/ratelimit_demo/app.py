import asyncio
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager

import aiomysql
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .config import Limits, Settings, load_limits
from .database import Database
from .limiters import Limiters
from .load import LoadRunner
from .models import SCENARIOS, QueryRequest, RunRequest, new_snapshot
from .store import RunCapacityError, RunStore

logger = logging.getLogger("ratelimit_demo")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def create_app(
    settings: Settings | None = None,
    limits: Limits | None = None,
    database: Database | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    limits = limits or load_limits()
    if not 0 < settings.query_timeout <= 5 or settings.lease_ttl_ms <= 10000:
        raise ValueError("Query timeout must be <=5s and concurrency lease TTL >10s")
    db = database or Database(settings)
    cache = redis or Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=2,
    )
    limiters = Limiters(cache, limits, settings.lease_ttl_ms)
    store = RunStore(cache, settings.run_ttl, settings.max_runs, settings.heartbeat_ttl)
    runner = LoadRunner(store, settings.target_urls, settings.load_url)
    workers: dict[str, asyncio.Task] = {}
    registry = CollectorRegistry()
    requests = Counter(
        "limiter_requests_total",
        "Completed HTTP query requests",
        ["query_id", "mode", "status"],
        registry=registry,
    )
    successes = Counter(
        "limiter_success_total", "Successful queries", ["query_id", "mode"], registry=registry
    )
    rejections = Counter(
        "limiter_rejections_total",
        "Limit rejections",
        ["query_id", "mode", "reason"],
        registry=registry,
    )
    active = Gauge(
        "limiter_sql_active",
        "Active protected SQL operations including controlled delay, on this node",
        ["query_id", "mode"],
        registry=registry,
    )
    queued = Histogram(
        "limiter_queue_seconds", "Limiter admission wait", ["query_id", "mode"], registry=registry
    )
    duration = Histogram(
        "limiter_sql_seconds",
        "Protected SQL operation duration, including controlled delay",
        ["query_id", "mode"],
        registry=registry,
    )
    db_errors = Counter(
        "limiter_database_errors_total",
        "Database or ProxySQL connection/execution errors",
        ["query_id"],
        registry=registry,
    )
    redis_errors = Counter("limiter_redis_errors_total", "Shared state failures", registry=registry)
    local_active: dict[tuple[str, str], int] = {}

    async def readiness() -> dict:
        state = await db.health()
        try:
            redis_ok = bool(await cache.ping())
        except (RedisError, OSError):
            redis_ok = False
        return {
            "node_id": settings.node_id,
            **state,
            "redis": redis_ok,
            "ready": bool(state["proxysql"] and state["mysql"] and redis_ok),
            "limiter_mode": settings.mode,
        }

    async def heartbeat() -> None:
        while True:
            try:
                await store.heartbeat(await readiness())
            except (RedisError, OSError, TimeoutError):
                logger.warning(
                    json.dumps({"event": "heartbeat_unavailable", "node_id": settings.node_id})
                )
            await asyncio.sleep(2)

    def worker_finished(run_id: str, task: asyncio.Task) -> None:
        workers.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(json.dumps({"event": "run_worker_failed", "run_id": run_id}))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        beat = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            beat.cancel()
            pending = list(workers.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(beat, *pending, return_exceptions=True)
            await db.close()
            if redis is None:
                await cache.aclose()

    app = FastAPI(title="SQL Rate Limit Demo", lifespan=lifespan)
    app.state.settings, app.state.limits = settings, limits
    app.state.store, app.state.limiters = store, limiters
    app.state.database, app.state.workers = db, workers
    app.state.registry = registry
    if (settings.web_root / "static").is_dir():
        app.mount("/static", StaticFiles(directory=settings.web_root / "static"), name="static")

    @app.exception_handler(RedisError)
    async def unavailable(_: Request, exc: RedisError):
        redis_errors.inc()
        return JSONResponse(
            status_code=503,
            content={
                "node_id": settings.node_id,
                "reason": "redis_unavailable",
            },
        )

    @app.get("/")
    async def index():
        path = settings.web_root / "templates/index.html"
        if not path.is_file():
            raise HTTPException(503, "web_assets_unavailable")
        return FileResponse(path)

    @app.get("/healthz")
    async def health():
        return {"status": "alive", "node_id": settings.node_id}

    @app.get("/readyz")
    async def ready():
        state = await readiness()
        return JSONResponse(state, status_code=200 if state["ready"] else 503)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @app.get("/demo/config")
    async def config():
        state = await readiness()
        try:
            await store.heartbeat(state)
            nodes = await store.nodes()
        except RedisError:
            state["error"] = "redis_unavailable"
            nodes = [state]
        return {
            "node_id": settings.node_id,
            "limiter_mode": settings.mode,
            "public_url": settings.public_url,
            "limits": limits.model_dump(),
            "bounds": {
                "duration_seconds": {"min": 1, "max": 60},
                "qps": {"min": 1, "max": 100},
                "concurrency": {"min": 1, "max": 32},
            },
            "scenarios": [{"id": key, **value} for key, value in SCENARIOS.items()],
            "nodes": nodes,
        }

    @app.post("/query")
    async def query(body: QueryRequest):
        mode = body.limiter_mode or settings.mode
        qid = body.query_id
        started = time.monotonic()
        status = 503
        lease = None
        reason = ""
        try:
            wait_start = time.monotonic()
            decision = await (limiters.qps(mode) if qid == "qps_demo" else limiters.acquire(mode))
            queued.labels(qid, mode).observe(time.monotonic() - wait_start)
            if not decision.allowed:
                status = 429
                reason = "qps_limit_exceeded" if qid == "qps_demo" else "concurrency_limit_exceeded"
                rejections.labels(qid, mode, reason).inc()
                return JSONResponse(
                    status_code=429,
                    headers={
                        "Retry-After": str(max(1, math.ceil(decision.retry_after))),
                    },
                    content={
                        "node_id": settings.node_id,
                        "query_id": qid,
                        "reason": reason,
                        "retry_after_ms": math.ceil(decision.retry_after * 1000),
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                    },
                )
            lease = decision.lease_id
            key = (qid, mode)
            local_active[key] = local_active.get(key, 0) + 1
            sql_concurrency = decision.active if lease else local_active[key]
            active.labels(qid, mode).inc()
            execution_start = time.monotonic()
            try:
                async with asyncio.timeout(settings.query_timeout):
                    if qid == "concurrency_demo":
                        await asyncio.sleep(1)
                    rows = await db.query(qid, body.customer_id)
            finally:
                active.labels(qid, mode).dec()
                local_active[key] -= 1
                duration.labels(qid, mode).observe(time.monotonic() - execution_start)
            # Release before returning: a failed release is visible as a 503, never a false 200.
            if lease:
                await limiters.release(mode, lease)
                lease = None
            status = 200
            successes.labels(qid, mode).inc()
            return {
                "node_id": settings.node_id,
                "query_id": qid,
                "limiter_mode": mode,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "sql_concurrency": sql_concurrency,
                "rows": rows,
            }
        except (RedisError, OSError, aiomysql.Error, TimeoutError) as exc:
            reason = "redis_unavailable" if isinstance(exc, RedisError) else "database_unavailable"
            if isinstance(exc, RedisError):
                redis_errors.inc()
            else:
                db_errors.labels(qid).inc()
            return JSONResponse(
                status_code=503,
                content={
                    "node_id": settings.node_id,
                    "query_id": qid,
                    "reason": reason,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                },
            )
        finally:
            if lease:
                try:
                    await asyncio.shield(limiters.release(mode, lease))
                except (RedisError, OSError):
                    redis_errors.inc()
                    logger.warning(json.dumps({"event": "lease_release_failed", "query_id": qid}))
            requests.labels(qid, mode, str(status)).inc()
            logger.info(
                json.dumps(
                    {
                        "event": "query",
                        "node_id": settings.node_id,
                        "query_id": qid,
                        "mode": mode,
                        "status": status,
                        "reason": reason,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    }
                )
            )

    def checked_id(run_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(404, "run_not_found")
        return run_id

    @app.post("/demo/runs", status_code=202)
    async def start_run(body: RunRequest):
        if body.scenario_id == "failover":
            raise HTTPException(403, "failover_disabled_use_protected_admin_script")
        snapshot = new_snapshot(body, body.limiter_mode or settings.mode)
        try:
            await store.create(snapshot)
        except RunCapacityError:
            raise HTTPException(429, "global_run_capacity_exceeded", headers={"Retry-After": "5"})
        task = asyncio.create_task(runner.run(snapshot))
        workers[snapshot["run_id"]] = task
        task.add_done_callback(lambda done: worker_finished(snapshot["run_id"], done))
        return snapshot

    @app.get("/demo/runs/{run_id}")
    async def get_run(run_id: str):
        snapshot = await store.get(checked_id(run_id))
        if snapshot is None:
            raise HTTPException(404, "run_not_found_or_expired")
        return snapshot

    @app.post("/demo/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        snapshot = await store.cancel(checked_id(run_id))
        if snapshot is None:
            raise HTTPException(404, "run_not_found_or_expired")
        return snapshot

    @app.get("/demo/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request):
        checked_id(run_id)
        snapshot = await store.get(run_id)
        if snapshot is None:
            raise HTTPException(404, "run_not_found_or_expired")
        last = request.headers.get("last-event-id", "0")
        if not re.fullmatch(r"\d{1,12}(?:-0)?", last):
            raise HTTPException(400, "invalid_last_event_id")
        after = last if "-" in last else last + "-0"
        latest = int(await cache.get(store.key(run_id, "seq")) or 0)
        if int(after.split("-")[0]) > latest:
            raise HTTPException(400, "last_event_id_ahead_of_stream")

        async def stream():
            nonlocal after
            while not await request.is_disconnected():
                try:
                    current = await store.get(run_id)
                    if current is None:
                        yield 'event: error\ndata: {"reason":"run_expired","poll":true}\n\n'
                        return
                    events = await store.events(run_id, after)
                    for event_id, fields in events:
                        after = event_id
                        yield f"id: {event_id.split('-')[0]}\nevent: {fields['event']}\ndata: {fields['data']}\n\n"
                        if fields["event"] == "complete":
                            return
                    if not events and current["status"] != "running":
                        return
                    if not events:
                        yield ": heartbeat\n\n"
                except RedisError:
                    yield 'event: error\ndata: {"reason":"redis_unavailable","poll":true}\n\n'
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
