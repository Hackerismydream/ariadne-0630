"""Clean-checkout v1 demo path."""

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.cli import app as cli_app
from ariadne.store import Store


def test_demo_v1_creates_inspectable_runtime_facts(tmp_path, monkeypatch):
    demo_dir = tmp_path / "demo"
    db_path = demo_dir / "ariadne-v1.db"
    runner = CliRunner()

    result = runner.invoke(
        cli_app,
        ["demo-v1", "--output-dir", str(demo_dir), "--reset"],
    )
    assert result.exit_code == 0
    assert "Ariadne Managed Agent Team Runtime v1 demo complete" in result.stdout
    assert "dry-run=completed" in result.stdout
    assert "live-execution=skipped" in result.stdout

    store = Store(str(db_path))
    try:
        assert len(store.list_runtime_machines()) >= 1
        assert len(store.list_runtime_capabilities()) >= 1
        assert len(store.list_taskruns()) >= 1
        assert len(store.list_runtime_leases()) >= 1
        assert len(store.list_leader_decisions()) >= 2
        assert len(store.list_benchmark_runs()) >= 1
        benchmark = store.list_benchmark_runs()[0]
        assert benchmark.summary["success"] is True
    finally:
        store.close()

    env = {"ARIADNE_DB": str(db_path)}
    for command in (
        ["runtime-list"],
        ["capability-list"],
        ["taskrun-list"],
        ["runtime-lease-list"],
        ["leader-decision-list"],
        ["benchmark-list"],
    ):
        result = runner.invoke(cli_app, command, env=env)
        assert result.exit_code == 0
        assert "No " not in result.stdout

    monkeypatch.setattr("ariadne.api._db_path", str(db_path))
    client = TestClient(app)
    assert client.get("/api/runtime-machines").json()
    assert client.get("/api/runtime-capabilities").json()
    assert client.get("/api/runtime-leases").json()
    assert client.get("/api/leader-decisions").json()
    assert client.get("/api/benchmark-runs").json()
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "RuntimeLeases" in dashboard.text
    assert "LeaderDecisions" in dashboard.text
    assert "BenchmarkRuns" in dashboard.text
