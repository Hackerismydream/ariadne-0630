"""IssueTimeline product event stream tests."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.backends import get_backend
from ariadne.cli import app as cli_app
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType, FailureReason, IssueStatus
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def seed_agent_issue(store: Store):
    agent = store.create_agent("Runner", "", ["dry-run"], [])
    issue = store.create_issue("Timeline work", "ship it", AssigneeType.AGENT, agent.id)
    return agent, issue


def test_dry_run_daemon_writes_ordered_issue_timeline(store: Store, tmp_path):
    agent, issue = seed_agent_issue(store)
    taskrun = store.enqueue_taskrun(issue.id, agent.id)
    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="rt-timeline",
        poll_interval=0.01,
        target_repo_path=str(tmp_path),
    )

    daemon.start(max_iterations=1)

    timeline = store.get_issue_timeline(issue.id)
    event_types = [event.event_type for event in timeline]
    assert event_types == [
        "issue_created",
        "taskrun_queued",
        "lease_acquired",
        "taskrun_preparing",
        "taskrun_started",
        "progress_reported",
        "taskrun_completed",
        "lease_released",
    ]
    assert timeline[1].taskrun_id == taskrun.id
    assert timeline[2].runtime_lease_id is not None
    assert timeline[5].payload["summary"] == "dry-run: simulated execution"

    trace_events = store.get_timeline(taskrun.trace_id)
    assert trace_events


def test_issue_timeline_records_retry_cancel_and_closure(store: Store):
    agent, issue = seed_agent_issue(store)
    taskrun = store.enqueue_taskrun(issue.id, agent.id)
    claimed = store.claim_task(agent.id, "legacy-runtime")
    assert claimed is not None
    store.start_task(claimed.id)
    store.fail_task(claimed.id, "bad", FailureReason.AGENT_ERROR)

    retry = store.retry_taskrun(taskrun.id)
    store.cancel_taskrun(retry.id)
    store.update_issue_status(issue.id, IssueStatus.DONE)

    event_types = [event.event_type for event in store.get_issue_timeline(issue.id)]
    assert event_types == [
        "issue_created",
        "taskrun_queued",
        "taskrun_started",
        "taskrun_failed",
        "retry_scheduled",
        "taskrun_queued",
        "taskrun_cancelled",
        "issue_closed",
    ]


def test_issue_timeline_cli_and_api_surfaces(tmp_path, monkeypatch):
    db = str(tmp_path / "timeline.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    monkeypatch.setattr("ariadne.api._db_path", db)
    s = Store(db)
    agent, issue = seed_agent_issue(s)
    s.enqueue_taskrun(issue.id, agent.id)
    s.close()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["issue-timeline", issue.id])
    assert result.exit_code == 0
    assert "issue_created" in result.stdout
    assert "taskrun_queued" in result.stdout

    client = TestClient(app)
    assert "IssueTimeline" in client.get("/").text

    res = client.get(f"/api/issues/{issue.id}/timeline")
    assert res.status_code == 200
    data = res.json()
    assert [event["event_type"] for event in data] == [
        "issue_created",
        "taskrun_queued",
    ]
