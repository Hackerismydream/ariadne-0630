"""Tests for daemon poll-claim-execute loop + CLI.

Per docs/plan/tasks/core-003.md "test_daemon.py must cover".
"""

import shlex
import sys
import re

import pytest
from typer.testing import CliRunner

from ariadne.backends import get_backend
from ariadne.cli import app
from ariadne.daemon import Daemon
from ariadne.models import (
    AssigneeType,
    ExecutionResult,
    FailureReason,
    TaskStatus,
)
from ariadne.runner import run_intent
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def daemon(store):
    return Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="test-rt",
        poll_interval=0.01,
        stale_claim_timeout=60.0,
    )


@pytest.fixture
def agent_with_task(store):
    """Create an agent + issue + enqueued task, return (agent, task)."""
    agent = store.create_agent("TestAgent", "do things", ["dry-run"], [])
    issue = store.create_issue("test issue", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    return agent, task


# ---------------------------------------------------------------------------
# Poll + claim
# ---------------------------------------------------------------------------


def test_poll_claims_queued_task(daemon, agent_with_task):
    """enqueue task → poll_once → task becomes preparing via RuntimeLease"""
    agent, task = agent_with_task
    claimed = daemon._poll_once()
    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.status == TaskStatus.PREPARING


def test_poll_returns_none_when_no_tasks(daemon, store):
    """No queued tasks → poll_once returns None"""
    assert daemon._poll_once() is None


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def test_execute_completes_task(daemon, agent_with_task, store):
    """claimed task → execute → completed with result"""
    agent, task = agent_with_task
    claimed = daemon._poll_once()
    daemon._execute_task(claimed)

    finished = store.get_task(claimed.id)
    assert finished.status == TaskStatus.COMPLETED
    assert finished.result is not None
    assert finished.result["backend_name"] == "dry-run"
    assert finished.completed_at is not None


def test_execute_fails_task(daemon, store):
    """backend returns failure → task failed with reason"""
    class FailingBackend:
        name = "failing"

        def is_available(self):
            return True

        def execute(self, context, on_progress=None):
            return ExecutionResult(
                backend_name="failing",
                success=False,
                exit_code=1,
                stdout="",
                stderr="something went wrong",
                diff=None,
                changed_files=[],
                test_result=None,
                failure_reason=FailureReason.AGENT_ERROR,
                duration_seconds=0.1,
                command="failing",
            )

    agent = store.create_agent("FailAgent", "", ["failing"], [])
    issue = store.create_issue("fail test", "", AssigneeType.AGENT, agent.id)
    store.enqueue_task(issue.id, agent.id)

    failing_daemon = Daemon(
        store=store,
        backend_factory=lambda name: FailingBackend() if name == "failing" else get_backend(name),
        poll_interval=0.01,
    )
    claimed = failing_daemon._poll_once()
    failing_daemon._execute_task(claimed)

    failed = store.get_task(claimed.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.failure_reason == FailureReason.AGENT_ERROR


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_on_failure(daemon, store):
    """fail + attempt < max → new queued task created"""
    class FailBackend:
        name = "failing"
        def is_available(self): return True
        def execute(self, ctx, on_progress=None):
            return ExecutionResult(
                backend_name="failing", success=False, exit_code=1,
                stdout="", stderr="err", diff=None, changed_files=[],
                test_result=None, failure_reason=FailureReason.AGENT_ERROR,
                duration_seconds=0.0, command="fail",
            )

    agent = store.create_agent("A", "", ["failing"], [])
    issue = store.create_issue("retry test", "", AssigneeType.AGENT, agent.id)
    store.enqueue_task(issue.id, agent.id)

    d = Daemon(store=store, backend_factory=lambda n: FailBackend(), poll_interval=0.01)
    claimed = d._poll_once()
    d._execute_task(claimed)

    assert store.get_task(claimed.id).status == TaskStatus.FAILED

    tasks = store._conn.execute(
        "SELECT * FROM task WHERE parent_task_id = ?", (claimed.id,)
    ).fetchall()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "queued"
    assert tasks[0]["attempt"] == 2


def test_no_retry_when_exhausted(store):
    """fail + attempt == max → no new task"""
    class FailBackend:
        name = "failing"
        def is_available(self): return True
        def execute(self, ctx, on_progress=None):
            return ExecutionResult(
                backend_name="failing", success=False, exit_code=1,
                stdout="", stderr="err", diff=None, changed_files=[],
                test_result=None, failure_reason=FailureReason.AGENT_ERROR,
                duration_seconds=0.0, command="fail",
            )

    agent = store.create_agent("A", "", ["failing"], [])
    issue = store.create_issue("no retry", "", AssigneeType.AGENT, agent.id)

    store.enqueue_task(issue.id, agent.id)
    d = Daemon(store=store, backend_factory=lambda n: FailBackend(), poll_interval=0.01)
    claimed1 = d._poll_once()
    d._execute_task(claimed1)
    assert store.get_task(claimed1.id).status == TaskStatus.FAILED

    retry = store.retry_task(claimed1.id)
    assert retry.attempt == 2

    claimed2 = d._poll_once()
    d._execute_task(claimed2)
    assert store.get_task(claimed2.id).status == TaskStatus.FAILED

    retries = store._conn.execute(
        "SELECT * FROM task WHERE parent_task_id = ?", (claimed2.id,)
    ).fetchall()
    assert len(retries) == 0


# ---------------------------------------------------------------------------
# Stale claim recovery
# ---------------------------------------------------------------------------


def test_stale_claim_recovery(daemon, agent_with_task, store):
    """old claimed task → recovered to queued"""
    agent, task = agent_with_task
    claimed = store.claim_task(agent.id, "test-rt")
    assert claimed.status == TaskStatus.CLAIMED

    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    store._conn.execute("UPDATE task SET dispatched_at = ? WHERE id = ?", (old, claimed.id))
    store._conn.commit()

    recovered = daemon._recover_stale_claims()
    assert recovered == 1

    t = store.get_task(claimed.id)
    assert t.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_updates_state(daemon):
    """after heartbeat → daemon_state timestamp updated"""
    daemon._send_heartbeat()
    assert daemon._last_heartbeat is not None

    row = daemon.store._conn.execute(
        "SELECT value FROM daemon_state WHERE key = 'last_heartbeat'"
    ).fetchone()
    assert row is not None
    assert row["value"] == daemon._last_heartbeat.isoformat()


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_dry_run_backend_default(daemon, agent_with_task, store):
    """no backend specified → uses dry-run"""
    agent, task = agent_with_task
    claimed = daemon._poll_once()
    daemon._execute_task(claimed)

    finished = store.get_task(claimed.id)
    assert finished.status == TaskStatus.COMPLETED
    assert finished.result["backend_name"] == "dry-run"


def test_daemon_passes_resume_session_and_mcp_config_to_backend(store, tmp_path):
    """same-trace follow-up tasks resume the latest provider session."""
    backend_name = "capture-resume"
    captured_contexts = []

    class CapturingBackend:
        name = backend_name

        def is_available(self):
            return True

        def execute(self, context, on_progress=None):
            captured_contexts.append(context)
            return ExecutionResult(
                backend_name=backend_name,
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                diff=None,
                changed_files=[],
                failure_reason=None,
                duration_seconds=0.01,
                command="capture",
                command_cwd=str(tmp_path),
                execution_repo_path=str(tmp_path),
                session_id="sess-2",
            )

    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text("{}")
    profile = store.create_agent_profile(
        name="Continuity Agent",
        instructions="Continue prior provider sessions.",
        preferred_capabilities=[backend_name],
        runtime_policy={"mcp_config_path": str(mcp_path)},
    )
    issue = store.create_issue("Continue task", "", AssigneeType.AGENT, profile.id)
    trace_id = "trace-continuity"

    first = store.enqueue_taskrun(issue.id, profile.id, trace_id=trace_id)
    first_claimed = store.claim_task(profile.id, "rt-seed")
    assert first_claimed is not None
    store.start_task(first.id)
    store.complete_task(first.id, {"metadata": {"session_id": "sess-1"}})

    second = store.enqueue_taskrun(issue.id, profile.id, trace_id=trace_id)
    second_claimed = store.claim_task(profile.id, "rt-capture")
    assert second_claimed is not None

    daemon = Daemon(
        store=store,
        backend_factory=lambda name: CapturingBackend(),
        runtime_id="rt-capture",
        poll_interval=0.01,
        target_repo_path=str(tmp_path),
    )
    daemon._execute_task(second_claimed)

    assert store.get_task(second.id).status == TaskStatus.COMPLETED
    assert captured_contexts[0].resume_session_id == "sess-1"
    assert captured_contexts[0].mcp_config_path == str(mcp_path)


def test_skill_verification_records_evidence_without_failing_task(store, tmp_path):
    """Skill verification failure is persisted as evidence, not task failure."""
    backend_name = "capture-verification"
    captured_contexts = []

    class CapturingBackend:
        name = backend_name

        def is_available(self):
            return True

        def execute(self, context, on_progress=None):
            captured_contexts.append(context)
            return ExecutionResult(
                backend_name=backend_name,
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                diff=None,
                changed_files=[],
                failure_reason=None,
                duration_seconds=0.01,
                command="capture",
                command_cwd=str(tmp_path),
                execution_repo_path=str(tmp_path),
            )

    verification_command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('import sys; sys.exit(7)')}"
    )
    profile = store.create_agent_profile(
        name="Verification Agent",
        instructions="Run skill verification after execution.",
        preferred_capabilities=[backend_name],
    )
    skill = store.create_skill(
        name="verify-failure",
        description="Always fails for evidence testing.",
        tools_allowed=[backend_name],
        test_command=verification_command,
    )
    store.bind_skill_to_agent_profile(profile.id, skill.id)
    issue = store.create_issue("Verify evidence", "", AssigneeType.AGENT, profile.id)
    task = store.enqueue_taskrun(issue.id, profile.id)
    claimed = store.claim_task(profile.id, "rt-verify")
    assert claimed is not None

    daemon = Daemon(
        store=store,
        backend_factory=lambda name: CapturingBackend(),
        runtime_id="rt-verify",
        poll_interval=0.01,
        target_repo_path=str(tmp_path),
    )
    daemon._execute_task(claimed)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.COMPLETED
    assert captured_contexts[0].test_command is None
    verification = finished.result["skill_verifications"][0]
    assert verification["skill_name"] == "verify-failure"
    assert verification["passed"] is False
    assert verification["exit_code"] == 7
    assert any(
        event["event"] == "verification_failed"
        for event in store.get_timeline(task.trace_id)
    )
    issue_events = store.get_issue_timeline(issue.id)
    assert any(event.event_type == "tests_reported" for event in issue_events)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_runner():
    return CliRunner()


def test_cli_issue_create(cli_runner, tmp_path, monkeypatch):
    """cli creates issue visible in list"""
    db = str(tmp_path / "cli_test.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)

    result = cli_runner.invoke(app, ["agent-create", "--name", "CLI Agent", "--backend", "dry-run"])
    assert result.exit_code == 0

    result = cli_runner.invoke(app, ["agent-list"])
    assert result.exit_code == 0
    agent_line = [line for line in result.stdout.strip().split("\n") if "CLI Agent" in line][0]
    agent_id = agent_line.strip().split()[0]

    result = cli_runner.invoke(app, [
        "issue-create", "--title", "CLI Test Issue", "--assignee-id", agent_id,
    ])
    assert result.exit_code == 0
    assert "Created issue" in result.stdout

    result = cli_runner.invoke(app, ["issue-list"])
    assert result.exit_code == 0
    assert "CLI Test Issue" in result.stdout


def test_cli_daemon_start_exposes_write_workspace_instead_of_confirm(cli_runner):
    result = cli_runner.invoke(app, ["daemon-start", "--help"])
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

    assert result.exit_code == 0
    assert re.search(r"--write-\s*workspace", plain_output)
    assert "--confirm-execution" not in plain_output


def test_cli_daemon_start_max_iterations(cli_runner, tmp_path, monkeypatch):
    """daemon start --max-iterations 1 → runs once and exits"""
    db = str(tmp_path / "cli_daemon.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)

    cli_runner.invoke(app, ["agent-create", "--name", "D", "--backend", "dry-run"])
    result = cli_runner.invoke(app, ["agent-list"])
    agent_id = result.stdout.strip().split("\n")[0].strip().split()[0]

    cli_runner.invoke(app, ["issue-create", "--title", "Daemon Test", "--assignee-id", agent_id])

    result = cli_runner.invoke(app, ["daemon-start", "--max-iterations", "1", "--poll-interval", "0.01"])
    assert result.exit_code == 0
    assert "Daemon stopped" in result.stdout


def test_cli_daemon_start_handles_detached_squad_leader_task(
    cli_runner, tmp_path, monkeypatch
):
    db = str(tmp_path / "detached_squad.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    store = Store(db)
    try:
        result = run_intent(
            store,
            ["Queue real squad work"],
            backend="codex",
            squad=True,
            target_repo=str(tmp_path),
            detach=True,
        )
        assert result.issue_id is not None
        issue_id = result.issue_id
    finally:
        store.close()

    result = cli_runner.invoke(
        app,
        [
            "daemon-start",
            "--max-iterations",
            "1",
            "--poll-interval",
            "0.01",
            "--target-repo",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    store = Store(db)
    try:
        taskruns = store.list_taskruns_for_issue(issue_id)
    finally:
        store.close()
    assert [taskrun.status for taskrun in taskruns] == [
        TaskStatus.COMPLETED,
        TaskStatus.QUEUED,
    ]
