import copy
import json
import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError

from ratelimit_demo.cli import save_results, validate_result
from ratelimit_demo.config import Limits, Settings, http_url, load_limits
from ratelimit_demo.database import Database
from ratelimit_demo.load import LoadRunner, Measurements, percentiles
from ratelimit_demo.models import RunRequest, new_snapshot


def test_config_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("DATABASE_TLS", raising=False)
    assert Settings.from_env().database_tls is True
    monkeypatch.setenv("DATABASE_TLS", "false")
    assert Settings.from_env().database_tls is False
    monkeypatch.setenv("DATABASE_TLS", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()
    for invalid in (
        {"qps_demo": {"rate_per_second": 0}},
        {"qps_demo": {"burst": 0}},
        {"concurrency_demo": {"max_concurrency": 33}},
        {"concurrency_demo": {"queue_timeout_ms": -1}},
        {"arbitrary": {}},
    ):
        with pytest.raises(ValidationError):
            Limits.model_validate(invalid)


def test_tls_identity_defaults_and_explicit_pinned_ca(monkeypatch):
    monkeypatch.setenv("DATABASE_TLS", "true")
    monkeypatch.delenv("DATABASE_TLS_VERIFY_IDENTITY", raising=False)
    monkeypatch.delenv("DATABASE_CA", raising=False)
    settings = Settings.from_env()
    context = Database(settings).tls_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setenv("DATABASE_TLS_VERIFY_IDENTITY", "false")
    with pytest.raises(ValueError, match="DATABASE_CA is required"):
        Settings.from_env()
    monkeypatch.setenv("DATABASE_CA", ssl.get_default_verify_paths().cafile)
    context = Database(Settings.from_env()).tls_context()
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert Database(Settings(database_tls=False)).tls_context() is None
    with pytest.raises(ValueError, match="explicit ProxySQL CA"):
        Database(Settings(database_tls_verify_identity=False)).tls_context()


def test_explicit_missing_config_is_error(monkeypatch):
    monkeypatch.setenv("LIMITER_CONFIG", "limiter/tests/does-not-exist.yaml")
    with pytest.raises(FileNotFoundError):
        load_limits()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost",
        "http://:secret@localhost",
        "http://user@localhost",
        "http://localhost:0",
        "http://localhost:70000",
        "http://localhost?target=other",
    ],
)
def test_target_urls_are_server_owned_safe_configuration(url):
    with pytest.raises(ValueError):
        http_url(url)


def test_load_url_is_independent_of_display_and_direct_nodes(monkeypatch):
    monkeypatch.setenv("LIMITER_LOAD_URL", "http://loadbalancer:8080")
    monkeypatch.setenv("LIMITER_PUBLIC_URL", "https://display.example")
    monkeypatch.setenv("LIMITER_TARGET_URLS", "http://node-1:8080,http://node-2:8080")
    settings = Settings.from_env()
    runner = LoadRunner(None, settings.target_urls, settings.load_url)
    for scenario in (
        "qps_below",
        "qps_above",
        "concurrency_below",
        "concurrency_above",
        "failover",
    ):
        assert runner.targets_for(scenario) == ("http://loadbalancer:8080",)
    assert runner.targets_for("distributed") == ("http://node-1:8080", "http://node-2:8080")
    assert settings.public_url == "https://display.example"


def test_load_url_falls_back_to_legacy_public_url_only_when_unset(monkeypatch):
    monkeypatch.delenv("LIMITER_LOAD_URL", raising=False)
    monkeypatch.setenv("LIMITER_PUBLIC_URL", "http://legacy-loadbalancer:8080")
    assert Settings.from_env().load_url == "http://legacy-loadbalancer:8080"
    monkeypatch.setenv("LIMITER_LOAD_URL", "http://internal-loadbalancer:8080")
    assert Settings.from_env().load_url == "http://internal-loadbalancer:8080"


def test_cli_suite_json_preserves_each_snapshot(namespace):
    path = Path(__file__).resolve().parent / f".suite-{namespace}.json"
    results = [
        new_snapshot(RunRequest(scenario_id="qps_below"), "distributed"),
        new_snapshot(RunRequest(scenario_id="distributed"), "local"),
    ]
    try:
        save_results(results, path, all_scenarios=True)
        assert json.loads(path.read_text()) == {"schema_version": 1, "runs": results}
    finally:
        path.unlink(missing_ok=True)


def test_fault_measurement_keeps_failures_but_standard_cli_rejects_them():
    stats = Measurements()
    base = new_snapshot(RunRequest(scenario_id="qps_below"), "distributed")
    for code, reason in ((503, "database_unavailable"), (0, "transport_error"), (200, "")):
        stats.enter(0)
        stats.record(0, code, 10, "node-1", reason, 1)
    result = stats.snapshot(base)
    result["status"] = "completed"
    with pytest.raises(ValueError, match="unavailable"):
        validate_result(result, Limits().model_dump())
    validate_result(result, Limits().model_dump(), allow_unavailable=True)
    assert result["summary"]["unavailable"] == 2
    assert result["summary"]["status_codes"] == {"503": 1, "0": 1, "200": 1}


@pytest.mark.parametrize(
    "values, expected",
    [
        ([], {"p50": 0, "p95": 0, "p99": 0}),
        ([10], {"p50": 10, "p95": 10, "p99": 10}),
        ([0, 100], {"p50": 50, "p95": 95, "p99": 99}),
        ([3, 1, 2], {"p50": 2, "p95": 2.9, "p99": 2.98}),
    ],
)
def test_percentiles(values, expected):
    assert percentiles(values) == expected


def test_measurement_totals_and_cli_assertions():
    request = RunRequest(scenario_id="qps_above", duration_seconds=2, qps=30)
    snapshot = new_snapshot(request, "distributed")
    stats = Measurements()
    stats.enter(0)
    stats.enter(0)
    stats.record(0, 200, 10, "node-1", "", 1)
    stats.record(0, 429, 20, "node-2", "qps_limit_exceeded", 0)
    stats.enter(1)
    stats.record(1, 200, 30, "node-1", "", 1)
    result = stats.snapshot(snapshot)
    result["status"] = "completed"
    assert result["summary"]["total"] == 3
    assert result["summary"]["success_rate"] == 2 / 3
    assert result["summary"]["rate_limit_rate"] == 1 / 3
    assert result["summary"]["peak_concurrency"] == 2
    assert result["summary"]["latency_ms"]["p50"] == 20
    assert result["summary"]["nodes"] == {"node-1": 2, "node-2": 1}
    assert sum(bucket["total"] for bucket in result["buckets"]) == 3
    validate_result(result, Limits().model_dump())
    invalid = copy.deepcopy(result)
    invalid["summary"]["status_codes"]["503"] = 1
    with pytest.raises(ValueError):
        validate_result(invalid, Limits().model_dump())


def test_recovery_is_measured_not_assumed():
    stats = Measurements()
    base = new_snapshot(RunRequest(scenario_id="qps_below"), "distributed")
    stats.enter(0)
    stats.record(0, 503, 10, "node-1", "database_unavailable", 0, completed_ms=100)
    assert stats.snapshot(base)["summary"]["recovery_time_ms"] is None
    stats.enter(1)
    stats.record(1, 200, 10, "node-2", "", 1, completed_ms=350)
    assert stats.snapshot(base)["summary"]["recovery_time_ms"] == 250


def test_transport_failures_do_not_fabricate_http_503():
    stats = Measurements()
    base = new_snapshot(RunRequest(scenario_id="qps_below"), "distributed")
    stats.enter(0)
    stats.record(0, 0, 10, "", "transport_error", 0)
    stats.enter(0)
    stats.record(0, 200, 10, "", "invalid_response", 0)
    result = stats.snapshot(base)
    assert result["summary"]["status_codes"] == {"0": 1, "200": 1}
    assert result["summary"]["unavailable"] == 2
    assert result["summary"]["success"] == 0
    assert result["summary"]["errors"] == {"transport_error": 1, "invalid_response": 1}


@pytest.mark.parametrize(
    "update",
    [
        {"scenario_id": "unknown"},
        {"duration_seconds": 0},
        {"duration_seconds": 61},
        {"qps": 101},
        {"concurrency": 33},
        {"concurrency": True},
        {"limiter_mode": "invalid"},
        {"target_url": "http://example.com"},
        {"sql": "SELECT 1"},
    ],
)
def test_run_bounds(update):
    with pytest.raises(ValidationError):
        RunRequest.model_validate({"scenario_id": "qps_below", **update})
