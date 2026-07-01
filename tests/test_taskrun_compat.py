"""TaskRun compatibility tracer tests.

These tests define the first v1 vertical slice: new code can use TaskRun
language end-to-end while the legacy Task path remains intact.
"""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.backends import get_backend
from ariadne.cli import app as cli_app
from ariadne.daemon import Daemon
from ariadne import models
from ariadne.models import AssigneeType, FailureReason, Task, TaskStatus
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_taskrun_store_path_preserves_legacy_task_path(store: Store):
    assert hasattr(models, "TaskRun")
    TaskRun = models.TaskRun

    agent = store.create_agent("Coder", "", ["dry-run"], [])
    issue = store.create_issue("Implement slice", "", AssigneeType.AGENT, agent.id)

    taskrun = store.enqueue_taskrun(issue.id, agent.id, handoff_prompt="do it")

    assert isinstance(taskrun, TaskRun)
    assert taskrun.id.startswith("taskrun-")
    assert taskrun.agent_profile_id == agent.id
    assert taskrun.parent_taskrun_id is None
    assert taskrun.status == TaskStatus.QUEUED

    claimed = store.claim_taskrun(agent.id, "runtime-1")
    assert claimed is not None
    assert isinstance(claimed, TaskRun)
    assert claimed.id == taskrun.id

    store.start_taskrun(taskrun.id)
    failed = store.fail_taskrun(taskrun.id, "boom", FailureReason.AGENT_ERROR)
    assert failed.status == TaskStatus.FAILED

    retry = store.retry_taskrun(taskrun.id)
    assert isinstance(retry, TaskRun)
    assert retry.id.startswith("taskrun-")
    assert retry.parent_taskrun_id == taskrun.id
    assert retry.agent_profile_id == agent.id

    legacy = store.enqueue_task(issue.id, agent.id)
    assert isinstance(legacy, Task)
    assert legacy.id.startswith("task-")


def test_taskrun_can_execute_through_dry_run_daemon(store: Store):
    agent = store.create_agent("Runner", "", ["dry-run"], [])
    issue = store.create_issue("Run via daemon", "", AssigneeType.AGENT, agent.id)
    taskrun = store.enqueue_taskrun(issue.id, agent.id)

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="runtime-1",
        poll_interval=0.01,
    )

    claimed = daemon._poll_once()
    assert claimed is not None
    assert claimed.id == taskrun.id
    daemon._execute_task(claimed)

    completed = store.get_taskrun(taskrun.id)
    assert completed is not None
    assert completed.status == TaskStatus.COMPLETED
    assert completed.result["backend_name"] == "dry-run"


def test_taskrun_cli_commands_are_available(tmp_path, monkeypatch):
    db = str(tmp_path / "cli.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    runner = CliRunner()

    result = runner.invoke(cli_app, ["agent-create", "--name", "CLI Agent", "--backend", "dry-run"])
    assert result.exit_code == 0
    agent_id = runner.invoke(cli_app, ["agent-list"]).stdout.strip().split()[0]

    result = runner.invoke(
        cli_app,
        ["issue-create", "--title", "TaskRun CLI", "--assignee-id", agent_id],
    )
    assert result.exit_code == 0
    issue_id = result.stdout.split()[2]

    result = runner.invoke(cli_app, ["taskrun-create", issue_id, "--handoff", "ship it"])
    assert result.exit_code == 0
    assert "Created taskrun:" in result.stdout

    result = runner.invoke(cli_app, ["taskrun-list"])
    assert result.exit_code == 0
    assert "taskrun-" in result.stdout
    assert "agent_profile=" in result.stdout

    legacy = runner.invoke(cli_app, ["task-create", issue_id])
    assert legacy.exit_code == 0
    assert "Created task:" in legacy.stdout


def test_taskrun_api_endpoint_lists_taskruns(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setattr("ariadne.api._db_path", db)
    s = Store(db)
    agent = s.create_agent("API Agent", "", ["dry-run"], [])
    issue = s.create_issue("API TaskRun", "", AssigneeType.AGENT, agent.id)
    taskrun = s.enqueue_taskrun(issue.id, agent.id)
    s.close()

    client = TestClient(app)
    res = client.get("/api/taskruns")

    assert res.status_code == 200
    data = res.json()
    assert data == [
        {
            "id": taskrun.id,
            "issue_id": issue.id,
            "agent_profile_id": agent.id,
            "squad_id": None,
            "status": "queued",
            "attempt": 1,
            "trace_id": taskrun.trace_id,
            "created_at": taskrun.created_at.isoformat(),
        }
    ]
