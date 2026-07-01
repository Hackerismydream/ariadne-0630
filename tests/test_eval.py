"""Tests for eval.py — LLM-as-judge evaluation + benchmark harness.

Per docs/plan/tasks/eval-001.md "test_eval.py must cover".
"""

import pytest

from ariadne.eval import (
    BenchmarkTask,
    evaluate_task,
    report_to_dict,
    run_benchmark,
    run_single_vs_squad,
)
from ariadne.models import AssigneeType, FailureReason
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def completed_task(store):
    """Create a completed task for evaluation."""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("test", "desc", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    store.start_task(task.id)
    store.complete_task(task.id, {"output": "done"})
    return task


@pytest.fixture
def failed_task(store):
    """Create a failed task for evaluation."""
    agent = store.create_agent("B", "", ["dry-run"], [])
    issue = store.create_issue("fail", "desc", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    store.start_task(task.id)
    store.fail_task(task.id, "error", FailureReason.AGENT_ERROR)
    return task


# ---------------------------------------------------------------------------
# evaluate_task (deterministic fallback)
# ---------------------------------------------------------------------------


def test_evaluate_completed_task(store, completed_task):
    """completed task → score 5"""
    result = evaluate_task(store, completed_task.id)
    assert result.score == 5
    assert "completed" in result.reasoning.lower()


def test_evaluate_failed_task(store, failed_task):
    """failed task → score 1"""
    result = evaluate_task(store, failed_task.id)
    assert result.score == 1
    assert "failed" in result.reasoning.lower()


def test_evaluate_cancelled_task(store):
    """cancelled task → score 0"""
    agent = store.create_agent("C", "", ["dry-run"], [])
    issue = store.create_issue("cancel", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    store.cancel_task(task.id)

    result = evaluate_task(store, task.id)
    assert result.score == 0


def test_evaluate_missing_task(store):
    """task not found → raises KeyError"""
    with pytest.raises(KeyError):
        evaluate_task(store, "nonexistent")


# ---------------------------------------------------------------------------
# run_benchmark
# ---------------------------------------------------------------------------


def test_benchmark_run(store):
    """3 tasks → report with correct totals"""
    tasks = [
        BenchmarkTask(title="Task 1", description="desc 1", backend="dry-run", expected_success=True),
        BenchmarkTask(title="Task 2", description="desc 2", backend="dry-run", expected_success=True),
        BenchmarkTask(title="Task 3", description="desc 3", backend="dry-run", expected_success=True),
    ]
    report = run_benchmark(store, tasks)
    assert report.total_tasks == 3
    assert report.success_count == 3
    assert report.failure_count == 0


def test_benchmark_report_has_metrics(store):
    """report has success_count, avg_score, avg_duration, retry_count"""
    tasks = [BenchmarkTask(title="T", description="d", backend="dry-run", expected_success=True)]
    report = run_benchmark(store, tasks)
    assert hasattr(report, "success_count")
    assert hasattr(report, "avg_score")
    assert hasattr(report, "avg_duration_seconds")
    assert hasattr(report, "retry_count")


def test_benchmark_failure_breakdown(store):
    """mix of success/failure → failure_reasons dict populated"""
    def mock_execute(bt: BenchmarkTask) -> dict:
        if "fail" in bt.title:
            return {
                "task_id": "fake", "title": bt.title, "success": False,
                "duration_seconds": 0.0, "retry_count": 1,
                "failure_reason": "agent_error",
            }
        return {
            "task_id": "fake", "title": bt.title, "success": True,
            "duration_seconds": 0.5, "retry_count": 0,
            "failure_reason": None,
        }

    tasks = [
        BenchmarkTask(title="ok-1", description="", backend="dry-run", expected_success=True),
        BenchmarkTask(title="fail-1", description="", backend="dry-run", expected_success=False),
        BenchmarkTask(title="fail-2", description="", backend="dry-run", expected_success=False),
    ]
    report = run_benchmark(store, tasks, execute_fn=mock_execute)
    assert report.failure_count == 2
    assert "agent_error" in report.failure_reasons
    assert report.failure_reasons["agent_error"] == 2


def test_report_to_dict(store):
    """report serializes to dict for JSON output"""
    tasks = [BenchmarkTask(title="T", description="d", backend="dry-run", expected_success=True)]
    report = run_benchmark(store, tasks)
    d = report_to_dict(report)
    assert "total_tasks" in d
    assert "success_rate" in d
    assert "avg_score" in d
    assert d["total_tasks"] == 1


def test_single_vs_squad_compare_labels_dry_run_as_simulated(store):
    """dry-run comparison reports simulated evidence and bounded parallelism."""
    result = run_single_vs_squad(
        store,
        num_member_tasks=4,
        task_duration=0.001,
        backend="dry-run",
        max_concurrent=2,
    )

    assert result["backend"] == "dry-run"
    assert result["simulated"] is True
    assert result["status"] == "completed"
    assert result["max_concurrent"] == 2
    assert result["single"]["parallelism"] == 1
    assert result["single"]["task_count"] == 4
    assert result["squad"]["parallelism"] == 2
    assert result["squad"]["task_count"] == 4
    assert result["single"]["success"] is True
    assert result["squad"]["success"] is True


def test_single_vs_squad_compare_blocks_real_backend_without_gate(store, monkeypatch):
    """real backend comparison is truthful when external execution is disabled."""
    monkeypatch.delenv("ARIADNE_ENABLE_EXTERNAL_EXECUTION", raising=False)

    result = run_single_vs_squad(
        store,
        num_member_tasks=2,
        backend="codex",
        max_concurrent=4,
    )

    assert result["backend"] == "codex"
    assert result["simulated"] is False
    assert result["status"] == "blocked"
    assert result["max_concurrent"] == 2
    assert "ARIADNE_ENABLE_EXTERNAL_EXECUTION" in result["blocked_reason"]
    assert result["single"]["success"] is False
    assert result["squad"]["success"] is False
