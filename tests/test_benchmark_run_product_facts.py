"""BenchmarkRun records computed from product facts."""

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.cli import app as cli_app
from ariadne.eval import BenchmarkTask, report_to_dict, run_benchmark
from ariadne.store import Store


def test_dry_run_benchmark_records_product_facts(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    try:
        report = run_benchmark(
            store,
            [
                BenchmarkTask(
                    title="Case 1",
                    description="exercise the dry-run path",
                    backend="dry-run",
                    expected_success=True,
                    suite_name="runtime-v1",
                )
            ],
        )

        runs = store.list_benchmark_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.suite_name == "runtime-v1"
        assert run.case_name == "Case 1"
        assert run.issue_id
        assert run.status == "completed"
        assert run.summary["success"] is True
        assert run.artifact_dir
        assert run.metrics["taskrun_count"] >= 3
        assert run.metrics["runtime_lease_count"] >= 3
        assert run.metrics["leader_decision_count"] == 2
        assert run.metrics["issue_timeline_event_count"] > 0

        assert store.get_issue(run.issue_id) is not None
        assert store.get_issue_timeline(run.issue_id)
        assert store.list_leader_decisions(run.issue_id)
        assert report.total_tasks == 1
        assert report.success_count == 1
        assert report.benchmark_run_ids == [run.id]
        assert report.tasks[0]["benchmark_run_id"] == run.id
        assert report.tasks[0]["metrics"]["runtime_lease_count"] >= 3
    finally:
        store.close()


def test_benchmark_report_separates_failure_classes(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    try:
        def execute(bt: BenchmarkTask) -> dict:
            return {
                "task_id": "taskrun-failed",
                "title": bt.title,
                "success": False,
                "duration_seconds": 0.0,
                "retry_count": 0,
                "failure_reason": "policy_blocked",
            }

        report = run_benchmark(
            store,
            [
                BenchmarkTask(
                    title="Policy blocked",
                    description="blocked",
                    backend="codex",
                    expected_success=False,
                )
            ],
            execute_fn=execute,
        )

        assert report.failure_reasons == {"policy_blocked": 1}
        assert report.failure_classes == {"policy": 1}
        assert report_to_dict(report)["failure_classes"] == {"policy": 1}
    finally:
        store.close()


def test_benchmark_can_repeat_same_case_name(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    try:
        tasks = [
            BenchmarkTask(
                title="Repeat Case",
                description="same title twice",
                backend="dry-run",
                expected_success=True,
            ),
            BenchmarkTask(
                title="Repeat Case",
                description="same title twice",
                backend="dry-run",
                expected_success=True,
            ),
        ]
        report = run_benchmark(store, tasks)

        assert report.total_tasks == 2
        assert report.success_count == 2
        assert len(store.list_benchmark_runs()) == 2
    finally:
        store.close()


def test_benchmark_run_cli_api_and_dashboard_surfaces(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    monkeypatch.setattr("ariadne.api._db_path", db)

    store = Store(db)
    try:
        run_benchmark(
            store,
            [
                BenchmarkTask(
                    title="Surface Case",
                    description="show it",
                    backend="dry-run",
                    expected_success=True,
                )
            ],
        )
    finally:
        store.close()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["benchmark-list"])
    assert result.exit_code == 0
    assert "Surface Case" in result.stdout
    benchmark_run_id = result.stdout.strip().split()[0]

    client = TestClient(app)
    res = client.get("/api/benchmark-runs")
    assert res.status_code == 200
    assert res.json()[0]["id"] == benchmark_run_id
    assert res.json()[0]["metrics"]["leader_decision_count"] == 2

    res = client.get(f"/api/benchmark-runs/{benchmark_run_id}")
    assert res.status_code == 200
    assert res.json()["case_name"] == "Surface Case"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "BenchmarkRuns" in dashboard.text
