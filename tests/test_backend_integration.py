"""Tests for backend integration — handoff prompt passthrough, retry, CLI.

Per docs/plan/tasks/backend-002.md "test_backend_integration.py must cover".
"""

import pytest

from ariadne.daemon import Daemon
from ariadne.models import (
    AssigneeType,
    DelegationDecision,
    ExecutionResult,
    FailureReason,


)
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Handoff prompt passthrough
# ---------------------------------------------------------------------------


def test_handoff_prompt_stored_on_task(store):
    """orchestrator child task has handoff_prompt from DelegationDecision"""
    leader = store.create_agent("Leader", "", ["dry-run"], [])
    member = store.create_agent("Coder", "", ["dry-run"], ["python"])
    squad = store.create_squad("S", leader.id)
    store.add_squad_member(squad.id, member.id, role="coder")
    issue = store.create_issue("Build X", "desc", AssigneeType.SQUAD, squad.id)

    leader_task = store.enqueue_task(issue.id, leader.id, squad_id=squad.id)
    store.claim_task(leader.id, "rt-1")

    decision = DelegationDecision(
        target_agent_id=member.id,
        backend="dry-run",
        handoff_prompt="Please implement the X feature with tests",
        reason="best coder",
        skill_refs=["python"],
    )
    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: decision)
    orc.handle_leader_task(leader_task)

    # Child task should have the handoff_prompt
    child = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND squad_id = ?",
        (member.id, squad.id),
    ).fetchone()
    assert child["handoff_prompt"] == "Please implement the X feature with tests"


def test_daemon_uses_handoff_prompt(store):
    """ExecutionContext uses task.handoff_prompt when available"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("test", "", AssigneeType.AGENT, agent.id)

    # Enqueue with a specific handoff_prompt
    store.enqueue_task(issue.id, agent.id, handoff_prompt="Custom handoff instructions here")

    captured_contexts = []

    class CapturingBackend:
        name = "dry-run"
        def is_available(self): return True
        def execute(self, ctx, on_progress=None):
            captured_contexts.append(ctx)
            return ExecutionResult(
                backend_name="dry-run", success=True, exit_code=0,
                stdout="ok", stderr="", diff=None, changed_files=[],
                test_result=None, failure_reason=None,
                duration_seconds=0.0, command="dry-run",
            )

    daemon = Daemon(
        store=store,
        backend_factory=lambda n: CapturingBackend(),
        poll_interval=0.01,
    )
    daemon.start(max_iterations=1)

    assert len(captured_contexts) == 1
    assert captured_contexts[0].handoff_prompt == "Custom handoff instructions here"


# ---------------------------------------------------------------------------
# Retry with real failure
# ---------------------------------------------------------------------------


def test_retry_with_real_failure(store):
    """FailingBackend → retry creates new task"""
    class FailBackend:
        name = "failing"
        def is_available(self): return True
        def execute(self, ctx, on_progress=None):
            return ExecutionResult(
                backend_name="failing", success=False, exit_code=1,
                stdout="", stderr="crashed", diff=None, changed_files=[],
                test_result=None, failure_reason=FailureReason.AGENT_ERROR,
                duration_seconds=0.0, command="fail",
            )

    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("retry test", "", AssigneeType.AGENT, agent.id)
    store.enqueue_task(issue.id, agent.id)

    d = Daemon(store=store, backend_factory=lambda n: FailBackend(), poll_interval=0.01)
    d.start(max_iterations=1)

    # Original task failed, retry task created
    tasks = store._conn.execute("SELECT * FROM task").fetchall()
    assert len(tasks) == 2  # original + retry
    retry = [t for t in tasks if t["parent_task_id"] is not None]
    assert len(retry) == 1
    assert retry[0]["attempt"] == 2


# ---------------------------------------------------------------------------
# CLI backend passthrough
# ---------------------------------------------------------------------------


def test_agent_backend_from_cli(tmp_path, monkeypatch):
    """cli agent-create --backend codex → agent.backends == ["codex"]"""
    from typer.testing import CliRunner
    from ariadne.cli import app

    db = str(tmp_path / "cli.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)

    runner = CliRunner()
    result = runner.invoke(app, ["agent-create", "--name", "Codex Agent", "--backend", "codex"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["agent-list"])
    assert "codex" in result.stdout


# ---------------------------------------------------------------------------
# Squad loop with failing member
# ---------------------------------------------------------------------------


def test_squad_loop_with_failing_member(store):
    """member fails → retry → second attempt also fails → issue not done"""
    leader = store.create_agent("Leader", "", ["dry-run"], [])
    member = store.create_agent("Coder", "", ["dry-run"], ["python"])
    squad = store.create_squad("S", leader.id)
    store.add_squad_member(squad.id, member.id, role="coder")
    issue = store.create_issue("Build X", "desc", AssigneeType.SQUAD, squad.id)
    store.enqueue_task(issue.id, leader.id, squad_id=squad.id)

    class FailBackend:
        name = "failing"
        def is_available(self): return True
        def execute(self, ctx, on_progress=None):
            return ExecutionResult(
                backend_name="failing", success=False, exit_code=1,
                stdout="", stderr="always fails", diff=None, changed_files=[],
                test_result=None, failure_reason=FailureReason.AGENT_ERROR,
                duration_seconds=0.0, command="fail",
            )

    call_count = [0]

    def counting_decide(briefing, issue, completed_results=None):
        call_count[0] += 1
        from ariadne.models import DelegationDecision
        entry = briefing.roster[0]
        if call_count[0] <= 2:
            # First two activations: delegate (member will fail both times)
            return DelegationDecision(
                target_agent_id=entry.agent_id,
                backend=entry.backends[0],
                handoff_prompt="do it",
                reason=f"attempt {call_count[0]}",
                skill_refs=[],
            )
        # Third activation: give up
        return None

    orc = Orchestrator(store=store, llm_decide=counting_decide)
    daemon = Daemon(
        store=store,
        backend_factory=lambda n: FailBackend(),
        poll_interval=0.001,
        orchestrator=orc,
    )
    daemon.start(max_iterations=10)

    # Leader was activated multiple times (delegated twice, gave up on third)
    assert call_count[0] >= 3

    # Member should have failed tasks (both attempts failed)
    member_tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ?", (member.id,)
    ).fetchall()
    failed = [t for t in member_tasks if t["status"] == "failed"]
    assert len(failed) >= 1
