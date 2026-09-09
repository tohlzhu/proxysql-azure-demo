import asyncio
import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from .config import Limits, Mode

TOKEN_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'time')
local tokens = tonumber(state[1]) or tonumber(ARGV[2])
local previous = tonumber(state[2]) or now
tokens = math.min(tonumber(ARGV[2]), tokens + math.max(0, now-previous)*tonumber(ARGV[1]))
local ok = 0
local retry = 0
if tokens >= 1 then tokens = tokens - 1; ok = 1
else retry = (1-tokens)/tonumber(ARGV[1]) end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'time', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])/tonumber(ARGV[1])*2000)+1000)
return {ok, tostring(retry)}
"""

LEASE_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + tonumber(t[2])/1000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[1]) then return {0, count} end
redis.call('ZADD', KEYS[1], now+tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2])+1000)
return {1, count+1}
"""


@dataclass
class Decision:
    allowed: bool
    retry_after: float = 0
    lease_id: str | None = None
    active: int = 0


class Limiters:
    def __init__(
        self, redis: Redis, limits: Limits, lease_ttl_ms: int = 15000, namespace: str = "rl"
    ):
        self.redis = redis
        self.limits = limits
        self.lease_ttl_ms = lease_ttl_ms
        self.tokens = float(limits.qps_demo.burst)
        self.updated = time.monotonic()
        self.leases: set[str] = set()
        self.lock = asyncio.Lock()
        self.namespace = namespace

    async def qps(self, mode: Mode) -> Decision:
        config = self.limits.qps_demo
        if mode == "distributed":
            result = await self.redis.eval(
                TOKEN_SCRIPT,
                1,
                self.namespace + ":distributed:qps_demo",
                config.rate_per_second,
                config.burst,
            )
            return Decision(bool(result[0]), float(result[1]))
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(
                config.burst, self.tokens + (now - self.updated) * config.rate_per_second
            )
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return Decision(True)
            return Decision(False, (1 - self.tokens) / config.rate_per_second)

    async def acquire(self, mode: Mode) -> Decision:
        config = self.limits.concurrency_demo
        deadline = time.monotonic() + config.queue_timeout_ms / 1000
        lease = uuid.uuid4().hex
        while True:
            if mode == "distributed":
                result = await self.redis.eval(
                    LEASE_SCRIPT,
                    1,
                    self.namespace + ":distributed:concurrency_demo",
                    config.max_concurrency,
                    self.lease_ttl_ms,
                    lease,
                )
                if result[0]:
                    return Decision(True, lease_id=lease, active=int(result[1]))
            else:
                async with self.lock:
                    if len(self.leases) < config.max_concurrency:
                        self.leases.add(lease)
                        return Decision(True, lease_id=lease, active=len(self.leases))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Decision(False, retry_after=1)
            await asyncio.sleep(min(0.02, remaining))

    async def release(self, mode: Mode, lease: str) -> None:
        if mode == "distributed":
            await self.redis.zrem(self.namespace + ":distributed:concurrency_demo", lease)
        else:
            async with self.lock:
                self.leases.discard(lease)
