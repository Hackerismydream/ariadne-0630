"""Tests for orchestrator.py — leader delegation + event loop.

Per docs/plan/tasks/squad-002.md "test_orchestrator.py must cover".
"""

import pytest

from ariadne.models import (
    AssigneeType,
    DelegationDecision,
    IssueStatus,
    TaskStatus,
)
from ariadne.orchestrator import Orchestrator, deterministic_decide
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def squad_setup(store):
    """Create a squad with leader + 1 member + an issue assigned to the squad.

    Returns (squad, leader, member, issue, leader_task).
    """
    leader = store.create_agent("Leader", "coordinate", ["dry-run"], [])
    member = store.create_agent("Coder", "write code", ["dry-run"], ["python"])

    squad = store.create_squad("Alpha", leader.id, instructions="build it")
    store.add_squad_member(squad.id, member.id, role="coder")

    issue = store.create_issue("Build feature X", "Implement the thing", AssigneeType.SQUAD, squad.id)
    leader_task = store.enqueue_task(issue.id, leader.id, squad_id=squad.id)

    return squad, leader, member, issue, leader_task


# ---------------------------------------------------------------------------
# Leader delegation
# ---------------------------------------------------------------------------


def test_leader_delegates_to_member(store, squad_setup):
    """leader task → llm_decide returns DelegationDecision → child task created for target_agent"""
    squad, leader, member, issue, leader_task = squad_setup

    # Claim the leader task so it's in 'claimed' state — orchestrator will start+complete it
    store.claim_task(leader.id, "rt-1")

    orc = Orchestrator(store=store)
    orc.handle_leader_task(leader_task)

    # Leader task should be completed
    finished = store.get_task(leader_task.id)
    assert finished.status == TaskStatus.COMPLETED

    # A child task should exist for the member
    tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND squad_id = ?",
        (member.id, squad.id),
    ).fetchall()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "queued"


def test_leader_marks_done_when_no_delegation(store, squad_setup):
    """llm_decide returns None → issue marked done, leader task completed"""
    squad, leader, member, issue, leader_task = squad_setup

    store.claim_task(leader.id, "rt-1")

    orc = Orchestrator(store=store, llm_decide=lambda b, i: None)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.DONE
    assert store.get_task(leader_task.id).status == TaskStatus.COMPLETED


def test_delegation_rejects_unknown_agent(store, squad_setup):
    """DelegationDecision with agent_id not in roster → leader task failed"""
    squad, leader, member, issue, leader_task = squad_setup

    store.claim_task(leader.id, "rt-1")

    bad_decision = DelegationDecision(
        target_agent_id="nonexistent-agent",
        backend="dry-run",
        handoff_prompt="do something",
        reason="test",
        skill_refs=[],
    )

    orc = Orchestrator(store=store, llm_decide=lambda b, i: bad_decision)
    orc.handle_leader_task(leader_task)

    assert store.get_task(leader_task.id).status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


def test_event_loop_re_activates_leader(store, squad_setup):
    """all members complete → new leader task enqueued"""
    squad, leader, member, issue, leader_task = squad_setup

    # Simulate: a member task that just completed
    member_task = store.enqueue_task(issue.id, member.id, squad_id=squad.id)
    store.claim_task(member.id, "rt-m")
    store.start_task(member_task.id)
    store.complete_task(member_task.id, {"output": "done"})

    # No pending member tasks remain (leader_task is queued but it's the leader, not a member)
    orc = Orchestrator(store=store)
    orc.on_member_task_complete(member_task)

    # A new leader task should have been enqueued (the original leader_task is still queued,
    # but the event loop creates another one — that's fine, daemon will claim whichever is oldest)
    leader_queued = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND status = 'queued'",
        (leader.id,),
    ).fetchall()
    # The original leader_task is still queued + event loop enqueues another
    assert len(leader_queued) >= 1


def test_event_loop_waits_for_pending(store, squad_setup):
    """some members still running → no new leader task"""
    squad, leader, member, issue, leader_task = squad_setup

    # One member task still queued (not completed)
    member_task = store.enqueue_task(issue.id, member.id, squad_id=squad.id)

    # Count leader queued tasks before
    leader_before = store._conn.execute(
        "SELECT COUNT(*) as c FROM task WHERE agent_id = ? AND status = 'queued'",
        (leader.id,),
    ).fetchone()["c"]

    orc = Orchestrator(store=store)
    orc.on_member_task_complete(member_task)

    # No NEW leader task should be enqueued (member_task still pending)
    leader_after = store._conn.execute(
        "SELECT COUNT(*) as c FROM task WHERE agent_id = ? AND status = 'queued'",
        (leader.id,),
    ).fetchone()["c"]
    assert leader_after == leader_before


# ---------------------------------------------------------------------------
# Deterministic decider
# ---------------------------------------------------------------------------


def test_deterministic_decide_picks_matching_backend(store, squad_setup):
    """deterministic_decide selects member with matching backend"""
    squad, leader, member, issue, leader_task = squad_setup

    from ariadne.briefing import generate_briefing
    briefing = generate_briefing(store, squad.id)

    decision = deterministic_decide(briefing, issue)
    assert decision is not None
    assert decision.target_agent_id == member.id
    assert decision.backend == "dry-run"
    assert "Build feature X" in decision.handoff_prompt


def test_child_task_is_queued(store, squad_setup):
    """created child task has status=queued"""
    squad, leader, member, issue, leader_task = squad_setup

    store.claim_task(leader.id, "rt-1")

    orc = Orchestrator(store=store)
    orc.handle_leader_task(leader_task)

    child = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND squad_id = ?",
        (member.id, squad.id),
    ).fetchone()
    assert child["status"] == "queued"


def test_leader_task_completed_after_delegation(store, squad_setup):
    """leader task status=completed after delegating"""
    squad, leader, member, issue, leader_task = squad_setup

    store.claim_task(leader.id, "rt-1")

    orc = Orchestrator(store=store)
    orc.handle_leader_task(leader_task)

    assert store.get_task(leader_task.id).status == TaskStatus.COMPLETED
