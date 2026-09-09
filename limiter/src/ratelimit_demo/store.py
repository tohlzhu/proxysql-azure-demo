import json
import time

from redis.asyncio import Redis

from .models import utcnow

PREFIX = "rl:{runs}:"

CREATE = """
local t = redis.call('TIME')
local now = tonumber(t[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], now+90, ARGV[1])
redis.call('EXPIRE', KEYS[1], 100)
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[4])
redis.call('XADD', KEYS[4], '1-0', 'event', 'snapshot', 'data', ARGV[2])
redis.call('EXPIRE', KEYS[4], ARGV[4])
redis.call('SET', KEYS[5], '1', 'EX', ARGV[5])
return 1
"""

PUBLISH = """
local old = redis.call('GET', KEYS[1])
if not old then return 0 end
if cjson.decode(old).status ~= 'running' then return 0 end
local seq = redis.call('INCR', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('XADD', KEYS[3], tostring(seq)..'-0', 'event', ARGV[3], 'data', ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[2])
if ARGV[3] == 'complete' then
  redis.call('ZREM', KEYS[4], ARGV[4])
  redis.call('DEL', KEYS[5])
end
return seq
"""


class RunCapacityError(Exception):
    pass


class RunStore:
    def __init__(
        self,
        redis: Redis,
        ttl: int = 3600,
        cap: int = 2,
        heartbeat_ttl: int = 10,
        namespace: str = PREFIX,
    ):
        self.redis, self.ttl, self.cap, self.heartbeat_ttl = redis, ttl, cap, heartbeat_ttl
        self.prefix = namespace

    def key(self, run_id: str, suffix: str) -> str:
        return f"{self.prefix}{run_id}:{suffix}"

    async def create(self, snapshot: dict) -> None:
        run_id = snapshot["run_id"]
        ok = await self.redis.eval(
            CREATE,
            5,
            self.prefix + "active",
            self.key(run_id, "snapshot"),
            self.key(run_id, "seq"),
            self.key(run_id, "events"),
            self.key(run_id, "worker"),
            run_id,
            json.dumps(snapshot),
            self.cap,
            self.ttl,
            self.heartbeat_ttl,
        )
        if not ok:
            raise RunCapacityError("global_run_capacity_exceeded")

    async def publish(self, snapshot: dict) -> int:
        run_id = snapshot["run_id"]
        event = "snapshot" if snapshot["status"] == "running" else "complete"
        return int(
            await self.redis.eval(
                PUBLISH,
                5,
                self.key(run_id, "snapshot"),
                self.key(run_id, "seq"),
                self.key(run_id, "events"),
                self.prefix + "active",
                self.key(run_id, "worker"),
                json.dumps(snapshot),
                self.ttl,
                event,
                run_id,
            )
        )

    async def get(self, run_id: str, reconcile: bool = True) -> dict | None:
        raw = await self.redis.get(self.key(run_id, "snapshot"))
        if not raw:
            return None
        snapshot = json.loads(raw)
        if (
            reconcile
            and snapshot["status"] == "running"
            and not await self.redis.exists(self.key(run_id, "worker"))
        ):
            snapshot["status"] = "failed"
            snapshot["finished_at"] = utcnow()
            snapshot["summary"]["errors"]["worker_heartbeat_lost"] = 1
            await self.publish(snapshot)
            raw = await self.redis.get(self.key(run_id, "snapshot"))
            return json.loads(raw) if raw else None
        return snapshot

    async def pulse(self, run_id: str) -> None:
        await self.redis.set(self.key(run_id, "worker"), "1", ex=self.heartbeat_ttl)

    async def cancel(self, run_id: str) -> dict | None:
        snapshot = await self.get(run_id)
        if snapshot and snapshot["status"] == "running":
            await self.redis.set(self.key(run_id, "cancel"), "1", ex=self.ttl)
        return snapshot

    async def cancelled(self, run_id: str) -> bool:
        return bool(await self.redis.exists(self.key(run_id, "cancel")))

    async def events(self, run_id: str, after: str, block: int = 1000) -> list:
        streams = await self.redis.xread(
            {self.key(run_id, "events"): after}, count=100, block=block
        )
        return streams[0][1] if streams else []

    async def heartbeat(self, state: dict) -> None:
        node_id = state["node_id"]
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(self.prefix + "node:" + node_id, json.dumps(state), ex=self.heartbeat_ttl)
            pipe.zadd(self.prefix + "nodes", {node_id: time.time()})
            pipe.zremrangebyrank(self.prefix + "nodes", 0, -17)
            pipe.expire(self.prefix + "nodes", self.ttl)
            await pipe.execute()

    async def nodes(self) -> list[dict]:
        ids = await self.redis.zrange(self.prefix + "nodes", 0, -1)
        if not ids:
            return []
        values = await self.redis.mget([self.prefix + "node:" + node for node in ids])
        return [
            json.loads(value)
            if value
            else {
                "node_id": node,
                "ready": False,
                "proxysql": False,
                "mysql": False,
                "redis": False,
                "limiter_mode": "unknown",
                "error": "heartbeat_expired",
            }
            for node, value in zip(ids, values)
        ]
