import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from .config import Mode, QueryID, StrictModel

ScenarioID = Literal[
    "qps_below", "qps_above", "concurrency_below", "concurrency_above", "distributed", "failover"
]

SCENARIOS = {
    "qps_below": {
        "title": "Below QPS limit",
        "expected": "Requests below the refill rate succeed.",
        "defaults": {"duration_seconds": 10, "qps": 5, "concurrency": 4},
        "enabled": True,
    },
    "qps_above": {
        "title": "Above QPS limit",
        "expected": "The configured token bucket accepts its burst then returns 429.",
        "defaults": {"duration_seconds": 10, "qps": 30, "concurrency": 16},
        "enabled": True,
    },
    "concurrency_below": {
        "title": "Below concurrency limit",
        "expected": "Client concurrency below the configured SQL lease limit succeeds.",
        "defaults": {"duration_seconds": 10, "qps": 2, "concurrency": 2},
        "enabled": True,
    },
    "concurrency_above": {
        "title": "Above concurrency limit",
        "expected": "Only the configured SQL leases execute; excess clients queue then receive 429.",
        "defaults": {"duration_seconds": 10, "qps": 20, "concurrency": 12},
        "enabled": True,
    },
    "distributed": {
        "title": "Two-node global quota",
        "expected": "Traffic reaches both configured nodes; distributed QPS stays global, local QPS multiplies.",
        "defaults": {"duration_seconds": 10, "qps": 40, "concurrency": 16},
        "enabled": True,
    },
    "failover": {
        "title": "Protected node failover",
        "expected": "Disabled: use the administrator-controlled failover script.",
        "defaults": {"duration_seconds": 20, "qps": 5, "concurrency": 4},
        "enabled": False,
    },
}


class QueryRequest(StrictModel):
    query_id: QueryID
    limiter_mode: Mode | None = None
    customer_id: int = Field(default=1, ge=1, le=1000000, strict=True)


class RunRequest(StrictModel):
    scenario_id: ScenarioID
    duration_seconds: int = Field(default=10, ge=1, le=60, strict=True)
    qps: int = Field(default=5, ge=1, le=100, strict=True)
    concurrency: int = Field(default=4, ge=1, le=32, strict=True)
    limiter_mode: Mode | None = None

    @model_validator(mode="before")
    @classmethod
    def scenario_defaults(cls, value):
        if isinstance(value, dict) and isinstance(value.get("scenario_id"), str):
            scenario = SCENARIOS.get(value["scenario_id"])
            if scenario:
                return {**scenario["defaults"], **value}
        return value


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def empty_summary() -> dict:
    return {
        "total": 0,
        "success": 0,
        "rate_limited": 0,
        "unavailable": 0,
        "success_rate": 0.0,
        "rate_limit_rate": 0.0,
        "peak_concurrency": 0,
        "status_codes": {},
        "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0},
        "nodes": {},
        "errors": {},
        "recovery_time_ms": None,
        "transient_failures": 0,
        "peak_sql_concurrency": 0,
    }


def new_snapshot(request: RunRequest, mode: Mode) -> dict:
    return {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "scenario_id": request.scenario_id,
        "limiter_mode": mode,
        "status": "running",
        "parameters": request.model_dump(include={"duration_seconds", "qps", "concurrency"}),
        "expected": SCENARIOS[request.scenario_id]["expected"],
        "started_at": utcnow(),
        "finished_at": None,
        "summary": empty_summary(),
        "buckets": [],
    }
