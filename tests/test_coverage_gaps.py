"""Coverage gap tests — cover untested paths.

Per docs/plan/tasks/polish-002.md.
"""

import pytest

from ariadne.briefing import generate_briefing
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType, TaskStatus
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# cancel_task from various states
# ---------------------------------------------------------------------------


def test_cancel_from_queued(store):
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("t", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    cancelled = store.cancel_task(task.id)
    assert cancelled.status == TaskStatus.CANCELLED


def test_cancel_from_claimed(store):
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("t", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    cancelled = store.cancel_task(task.id)
    assert cancelled.status == TaskStatus.CANCELLED


def test_cancel_from_running(store):
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("t", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt-1")
    store.start_task(task.id)
    cancelled = store.cancel_task(task.id)
    assert cancelled.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_get_squad_leader_missing_squad(store):
    with pytest.raises(KeyError):
        store.get_squad_leader("nonexistent")


def test_briefing_missing_squad(store):
    with pytest.raises(KeyError):
        generate_briefing(store, "nonexistent")


def test_orchestrator_task_without_squad(store):
    """task with no squad_id → orchestrator logs error, returns gracefully"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("t", "", AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)
    # No squad_id — orchestrator should not crash
    orc = Orchestrator(store=store)
    orc.handle_leader_task(task)  # should return without crashing


# ---------------------------------------------------------------------------
# Daemon edge cases
# ---------------------------------------------------------------------------


def test_daemon_poll_no_agents(store):
    """daemon with no agents → poll_once returns None"""
    from ariadne.backends import get_backend
    daemon = Daemon(store=store, backend_factory=get_backend, poll_interval=0.01)
    assert daemon._poll_once() is None


# ---------------------------------------------------------------------------
# Full regression — verify all states reachable
# ---------------------------------------------------------------------------


def test_all_task_statuses_reachable(store):
    """Verify every TaskStatus can be reached through legal transitions"""
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("all-states", "", AssigneeType.AGENT, agent.id)

    # queued
    t = store.enqueue_task(issue.id, agent.id)
    assert t.status == TaskStatus.QUEUED

    # claimed
    t = store.claim_task(agent.id, "rt")
    assert t.status == TaskStatus.CLAIMED

    # running
    t = store.start_task(t.id)
    assert t.status == TaskStatus.RUNNING

    # completed
    t = store.complete_task(t.id, {"ok": True})
    assert t.status == TaskStatus.COMPLETED

    # failed (new task)
    t2 = store.enqueue_task(issue.id, agent.id)
    store.claim_task(agent.id, "rt2")
    store.start_task(t2.id)
    t2 = store.fail_task(t2.id, "err", __import__("ariadne.models", fromlist=["FailureReason"]).FailureReason.AGENT_ERROR)
    assert t2.status == TaskStatus.FAILED

    # cancelled (new task)
    t3 = store.enqueue_task(issue.id, agent.id)
    t3 = store.cancel_task(t3.id)
    assert t3.status == TaskStatus.CANCELLED
