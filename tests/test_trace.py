"""Tests for trace_id, activity_log, and timeline.

Per docs/plan/tasks/deep-001.md.
"""

import pytest

from ariadne.models import AssigneeType, FailureReason
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# trace_id generation
# ---------------------------------------------------------------------------


def test_trace_id_generated_on_enqueue(store):
    """enqueue_task → task has trace_id starting with 'trace-'"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("test", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    assert task.trace_id is not None
    assert task.trace_id.startswith("trace-")


def test_trace_id_inherited_on_retry(store):
    """retry → child has same trace_id as parent"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("test", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    store.start_task(task.id)
    store.fail_task(task.id, "err", FailureReason.AGENT_ERROR)
    retried = store.retry_task(task.id)
    assert retried.trace_id == task.trace_id


def test_trace_id_inherited_on_delegation(store):
    """orchestrator delegates → child task has leader's trace_id"""
    leader = store.create_agent("Leader", "", ["dry-run"], [])
    member = store.create_agent("Coder", "", ["dry-run"], ["python"])
    squad = store.create_squad("S", leader.id)
    store.add_squad_member(squad.id, member.id, role="coder")
    issue = store.create_issue("build", "", AssigneeType.SQUAD, squad.id)
    leader_task = store.enqueue_task(issue.id, leader.id, squad_id=squad.id)
    store.claim_task(leader.id, "rt-1")

    orc = Orchestrator(store=store)
    orc.handle_leader_task(leader_task)

    # Find child task
    child = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND squad_id = ?",
        (member.id, squad.id),
    ).fetchone()
    assert child["trace_id"] == leader_task.trace_id


# ---------------------------------------------------------------------------
# activity_log
# ---------------------------------------------------------------------------


def test_activity_log_records_transitions(store):
    """claim → start → complete → activity_log has entries"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("test", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    trace = task.trace_id

    store.claim_task(agent.id, "rt-1")
    store.start_task(task.id)
    store.complete_task(task.id, {"ok": True})

    events = store.get_timeline(trace)
    # enqueue logs "created", complete logs "completed" (via daemon, not store directly)
    # store.enqueue_task logs "created"
    assert any(e["event"] == "created" for e in events)


def test_get_timeline_returns_ordered(store):
    """timeline events are ordered by created_at"""
    trace = "trace-test-123"
    store.log_activity(trace, "task-1", "created", {"a": 1})
    store.log_activity(trace, "task-1", "claimed", {"b": 2})
    store.log_activity(trace, "task-1", "completed", {"c": 3})

    events = store.get_timeline(trace)
    assert len(events) == 3
    assert events[0]["event"] == "created"
    assert events[1]["event"] == "claimed"
    assert events[2]["event"] == "completed"
    assert events[0]["details"] == {"a": 1}


def test_log_activity_with_none_details(store):
    """log_activity with no details → details is None in timeline"""
    store.log_activity("trace-x", "task-1", "started")
    events = store.get_timeline("trace-x")
    assert len(events) == 1
    assert events[0]["details"] is None


# ---------------------------------------------------------------------------
# CLI task-timeline
# ---------------------------------------------------------------------------


def test_task_timeline_cli(tmp_path, monkeypatch):
    """ariadne task-timeline outputs events"""
    from typer.testing import CliRunner
    from ariadne.cli import app

    db = str(tmp_path / "cli.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)

    runner = CliRunner()
    # Create agent + issue + task
    runner.invoke(app, ["agent-create", "--name", "A", "--backend", "dry-run"])
    result = runner.invoke(app, ["agent-list"])
    agent_id = result.stdout.strip().split("\n")[0].strip().split()[0]
    runner.invoke(app, ["issue-create", "--title", "T", "--assignee-id", agent_id])
    issue_id = result.stdout  # not used
    # Get issue id from db
    import sqlite3
    conn = sqlite3.connect(db)
    issue_id = conn.execute("SELECT id FROM issue LIMIT 1").fetchone()[0]
    conn.close()
    runner.invoke(app, ["task-create", issue_id])

    # Get task id
    conn = sqlite3.connect(db)
    task_id = conn.execute("SELECT id FROM task LIMIT 1").fetchone()[0]
    conn.close()

    # Run timeline
    result = runner.invoke(app, ["task-timeline", task_id])
    assert result.exit_code == 0
    assert "trace-" in result.stdout
    assert "created" in result.stdout
