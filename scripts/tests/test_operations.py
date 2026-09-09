import json
import re
import runpy
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def prepare_artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "health.json").write_text('{"nodes": []}')
    (root / "proxysql-stats.jsonl").write_text('{"runtime_mysql_servers": []}\n')
    return root


def test_collect_nested_results_and_json_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = prepare_artifacts(tmp_path)
    directory = root / "previous-output.json"
    directory.mkdir()
    first = {"schema_version": 1, "run_id": "first", "summary": {"total": 1}}
    second = {"schema_version": 1, "run_id": "second", "summary": {"total": 2}}
    (directory / "qps.json").write_text(json.dumps(first))
    (root / "bundle.json").write_text(json.dumps({"schema_version": 1, "runs": [second]}))
    runpy.run_path(str(SCRIPTS / "collect-results.py"), run_name="__main__")
    result = json.loads((root / "collection.json").read_text())
    assert {item["run_id"]: item for item in result["runs"]} == {
        "first": first,
        "second": second,
    }


def test_collect_rejects_unknown_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = prepare_artifacts(tmp_path)
    (root / "run.json").write_text('{"schema_version": 9, "run_id": "invalid"}')
    with pytest.raises(ValueError, match="Unsupported result schema"):
        runpy.run_path(str(SCRIPTS / "collect-results.py"), run_name="__main__")


def test_proxy_template_renders_tls_and_keeps_credentials_private(tmp_path, monkeypatch):
    for key, value in {
        "MYSQL_HOST": "mysql",
        "MYSQL_APP_PASSWORD": "example-app-password",
        "MYSQL_MONITOR_PASSWORD": "example-monitor-password",
        "PROXYSQL_ADMIN_PASSWORD": "example-admin-password",
    }.items():
        monkeypatch.setenv(key, value)
    module = runpy.run_path(str(SCRIPTS / "render-proxysql.py"))
    output = tmp_path / "proxysql.cnf"
    module["render"](output)
    content = output.read_text()
    assert "use_ssl=1" in content
    assert 'mysql_ifaces="127.0.0.1:6032"' in content
    assert "${" not in content
    assert output.stat().st_mode & 0o777 == 0o600


def test_azure_collector_uses_exact_state_and_preserves_probe_data(tmp_path, monkeypatch):
    module = runpy.run_path(str(SCRIPTS / "collect-azure-results.py"))
    main = module["main"]
    main.__globals__["ROOT"] = tmp_path
    monkeypatch.setattr("sys.argv", ["collect-azure-results.py"])
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", "example-subscription")
    calls = []
    outputs = {
        "resource_group_name": "rg-dev2",
        "public_http_enabled": False,
        "http_url": "http://example.test:8080",
        "node_vm_names": {"node-1": "example-node-1", "node-2": "example-node-2"},
    }
    probe_data = [{"name": "DipAvailability", "timeseries": [{"data": [{"average": 100}]}]}]

    def az(arguments):
        calls.append(arguments)
        if arguments[:3] == ["monitor", "metrics", "list"]:
            return probe_data
        assert arguments[:3] == ["vm", "run-command", "invoke"]
        assert arguments[arguments.index("--name") + 1] in outputs["node_vm_names"].values()
        script = arguments[arguments.index("--scripts") + 1]
        assert "--stats-only" in script
        assert "--pull=never" in script
        return ['[stdout]\n{"runtime_mysql_servers": []}\n[stderr]\n']

    def state_scope(actual_outputs):
        assert actual_outputs == outputs
        return [
            {
                "type": "azurerm_lb",
                "name": "private",
                "values": {"name": "example-private-lb", "id": "example-lb-id"},
            }
        ]

    ops = SimpleNamespace(
        context=lambda: None,
        outputs=lambda: outputs,
        state_scope=state_scope,
        az=az,
        request_json=lambda base, path: (200, {"nodes": []}),
    )
    loader = SimpleNamespace(exec_module=lambda imported: None)
    monkeypatch.setattr(
        "importlib.util.spec_from_file_location", lambda name, path: SimpleNamespace(loader=loader)
    )
    monkeypatch.setattr("importlib.util.module_from_spec", lambda spec: ops)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: BytesIO(b"# test metrics\n"))
    main()
    status = json.loads((tmp_path / "artifacts/azure-status.json").read_text())
    assert status["load_balancers"][0]["probe_metrics"] == probe_data
    assert len(calls) == 3
    assert len((tmp_path / "artifacts/proxysql-stats.jsonl").read_text().splitlines()) == 2
    assert (tmp_path / "artifacts/metrics.prom").read_text() == "# test metrics\n"


def test_remote_verification_programs_parse_and_arm_recovery_before_outage():
    module = runpy.run_path(str(SCRIPTS / "azure-verify.py"))
    for name in ("PREPARE", "OBSERVE", "FAULT", "NODE_CHECK", "PACKAGE", "CHUNK"):
        script = module[name]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        for code in re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY(?:\n|$)", script, flags=re.S):
            compile(code, f"{name} embedded Python", "exec")
    fault = module["FAULT"]
    assert fault.index("trap 'docker start proxysql") < fault.index("docker stop proxysql")
    assert "sleep 20" in fault


@pytest.mark.parametrize("output", [None, "truncated response", "SIZE=1\nSIZE=2\n"])
def test_remote_result_markers_reject_missing_or_duplicate_output(output):
    module = runpy.run_path(str(SCRIPTS / "azure-verify.py"))
    with pytest.raises(RuntimeError, match="one complete remote"):
        module["marked"](output, "SIZE=")


def test_remote_result_marker_accepts_one_complete_value():
    module = runpy.run_path(str(SCRIPTS / "azure-verify.py"))
    assert module["marked"]("diagnostic\nCHUNK=YWJj\n", "CHUNK=") == "YWJj"
