import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["local", "distributed"]
QueryID = Literal["qps_demo", "concurrency_demo"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QPSLimit(StrictModel):
    rate_per_second: float = Field(default=10, gt=0, le=100)
    burst: int = Field(default=5, ge=1, le=100)


class ConcurrencyLimit(StrictModel):
    max_concurrency: int = Field(default=3, ge=1, le=32)
    queue_timeout_ms: int = Field(default=500, ge=0, le=2000)


class Limits(StrictModel):
    qps_demo: QPSLimit = Field(default_factory=QPSLimit)
    concurrency_demo: ConcurrencyLimit = Field(default_factory=ConcurrencyLimit)


class LimitsFile(StrictModel):
    limits: Limits = Field(default_factory=Limits)


def boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def http_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Limiter HTTP endpoints must be credential-free HTTP(S) URLs")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Limiter HTTP endpoint port is outside 1..65535")
    if parsed.query or parsed.fragment:
        raise ValueError("Limiter HTTP endpoints cannot include a query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    node_id: str = "node-1"
    mode: Mode = "distributed"
    redis_url: str = "redis://127.0.0.1:6379/0"
    database_host: str = "127.0.0.1"
    database_port: int = 6033
    database_user: str = "demo"
    database_password: str = ""
    database_name: str = "ratelimitdemo"
    database_tls: bool = True
    database_tls_verify_identity: bool = True
    database_ca: str | None = None
    target_urls: tuple[str, ...] = ("http://127.0.0.1:8080",)
    load_url: str = "http://127.0.0.1:8080"
    public_url: str | None = None
    web_root: Path = Path(__file__).resolve().parents[2]
    query_timeout: float = 4.0
    lease_ttl_ms: int = 15000
    run_ttl: int = 3600
    max_runs: int = 2
    heartbeat_ttl: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("LIMITER_MODE", "distributed")
        if mode not in {"distributed", "local"}:
            raise ValueError("LIMITER_MODE must be local or distributed")
        node = os.getenv("LIMITER_NODE_ID", "node-1")
        if not node or len(node) > 64 or not all(c.isalnum() or c in "-_" for c in node):
            raise ValueError(
                "LIMITER_NODE_ID must be 1-64 alphanumeric, dash or underscore characters"
            )
        port = int(os.getenv("DATABASE_PORT", "6033"))
        if not 1 <= port <= 65535:
            raise ValueError("DATABASE_PORT is outside 1..65535")
        database_tls = boolean("DATABASE_TLS", True)
        verify_identity = boolean("DATABASE_TLS_VERIFY_IDENTITY", True)
        database_ca = os.getenv("DATABASE_CA") or None
        if database_tls and not verify_identity and not database_ca:
            raise ValueError("DATABASE_CA is required when DATABASE_TLS_VERIFY_IDENTITY=false")
        redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        if urlsplit(redis_url).scheme not in {"redis", "rediss"}:
            raise ValueError("REDIS_URL must use redis or rediss")
        public_url = os.getenv("LIMITER_PUBLIC_URL") or None
        if public_url:
            public_url = http_url(public_url)
        load_url = http_url(os.getenv("LIMITER_LOAD_URL", public_url or "http://127.0.0.1:8080"))
        targets = tuple(
            http_url(u.strip())
            for u in os.getenv("LIMITER_TARGET_URLS", load_url).split(",")
            if u.strip()
        )
        if not 1 <= len(targets) <= 8:
            raise ValueError("LIMITER_TARGET_URLS needs 1..8 endpoints")
        return cls(
            node_id=node,
            mode=mode,
            redis_url=redis_url,
            database_host=os.getenv("DATABASE_HOST", "127.0.0.1"),
            database_port=port,
            database_user=os.getenv("DATABASE_USER", "demo"),
            database_password=os.getenv("DATABASE_PASSWORD", ""),
            database_name=os.getenv("DATABASE_NAME", "ratelimitdemo"),
            database_tls=database_tls,
            database_tls_verify_identity=verify_identity,
            database_ca=database_ca,
            target_urls=targets,
            load_url=load_url,
            public_url=public_url,
            web_root=Path(os.getenv("LIMITER_WEB_ROOT", str(cls.web_root))),
        )


def load_limits() -> Limits:
    explicit = os.getenv("LIMITER_CONFIG")
    path = (
        Path(explicit) if explicit else Path(__file__).resolve().parents[3] / "config/limits.yaml"
    )
    if not path.is_file() and not explicit:
        return Limits()
    with path.open(encoding="utf-8") as stream:
        return LimitsFile.model_validate(yaml.safe_load(stream) or {}).limits
