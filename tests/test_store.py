"""Tests for ariadne.store.Store — CRUD + basic lifecycle.

Covers every requirement in docs/plan/tasks/core-002.md `test_store.py must cover`:
- create_issue + get_issue round-trip
- create_agent + list_agents
- create_squad + add_squad_member + get_squad_members
- enqueue_task creates task with status=queued
- complete_task sets result and completed_at
- get_pending_member_tasks returns only non-terminal member tasks
"""

import pytest

from ariadne.models import AssigneeType, IssueStatus
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def test_create_issue_and_get_issue_round_trip(store: Store):
    issue = store.create_issue(
        title="Fix bug",
        description="Something is broken",
        assignee_type=AssigneeType.AGENT,
        assignee_id="agent-1",
    )
    assert issue.id.startswith("issue-")
    assert issue.title == "Fix bug"
    assert issue.description == "Something is broken"
    assert issue.status == IssueStatus.BACKLOG
    assert issue.assignee_type == AssigneeType.AGENT
    assert issue.assignee_id == "agent-1"
    assert issue.created_at is not None

    fetched = store.get_issue(issue.id)
    assert fetched is not None
    assert fetched == issue


def test_get_issue_returns_none_for_missing(store: Store):
    assert store.get_issue("nope") is None


def test_list_issues_returns_all(store: Store):
    i1 = store.create_issue("a", "", AssigneeType.AGENT, "agent-1")
    i2 = store.create_issue("b", "", AssigneeType.SQUAD, "squad-1")
    issues = store.list_issues()
    assert len(issues) == 2
    ids = {i.id for i in issues}
    assert {i1.id, i2.id} == ids


def test_update_issue_status(store: Store):
    issue = store.create_issue("t", "", AssigneeType.AGENT, "agent-1")
    updated = store.update_issue_status(issue.id, IssueStatus.IN_PROGRESS)
    assert updated.status == IssueStatus.IN_PROGRESS
    assert store.get_issue(issue.id).status == IssueStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def test_create_agent_and_list_agents(store: Store):
    a1 = store.create_agent(
        name="Coder",
        instructions="be useful",
        backends=["codex"],
        skills=["python"],
    )
    store.create_agent(
        name="Architect",
        instructions="design",
        backends=["claude-code"],
        skills=["system-design"],
    )
    assert a1.id.startswith("agent-")
    assert a1.backends == ["codex"]
    assert a1.skills == ["python"]

    agents = store.list_agents()
    assert len(agents) == 2
    names = {a.name for a in agents}
    assert names == {"Coder", "Architect"}


def test_get_agent_returns_none_for_missing(store: Store):
    assert store.get_agent("nope") is None


# ---------------------------------------------------------------------------
# Squad
# ---------------------------------------------------------------------------


def test_create_squad_add_member_get_members(store: Store):
    leader = store.create_agent("Leader", "lead", ["codex"], ["planning"])
    member = store.create_agent("Coder", "code", ["codex"], ["python"])

    squad = store.create_squad(name="Alpha", leader_id=leader.id, instructions="go")
    assert squad.id.startswith("squad-")
    assert squad.name == "Alpha"
    assert squad.leader_id == leader.id
    assert squad.instructions == "go"

    sm = store.add_squad_member(squad.id, member.id, role="coder")
    assert sm.squad_id == squad.id
    assert sm.member_id == member.id
    assert sm.role == "coder"
    assert sm.member_type == "agent"

    members = store.get_squad_members(squad.id)
    assert len(members) == 1
    assert members[0].member_id == member.id

    fetched_leader = store.get_squad_leader(squad.id)
    assert fetched_leader.id == leader.id


def test_get_squad_returns_none_for_missing(store: Store):
    assert store.get_squad("nope") is None


# ---------------------------------------------------------------------------
# Task lifecycle basics
# ---------------------------------------------------------------------------


def test_enqueue_task_creates_queued_task(store: Store):
    issue = store.create_issue("t", "", AssigneeType.AGENT, "agent-1")
    agent = store.create_agent("A", "", ["codex"], [])
    task = store.enqueue_task(issue.id, agent.id)
    assert task.status.value == "queued"
    assert task.issue_id == issue.id
    assert task.agent_id == agent.id
    assert task.attempt == 1
    assert task.max_attempts == 2
    assert task.parent_task_id is None
    assert task.created_at is not None


def test_complete_task_sets_result_and_completed_at(store: Store):
    issue = store.create_issue("t", "", AssigneeType.AGENT, "agent-1")
    agent = store.create_agent("A", "", ["codex"], [])
    store.enqueue_task(issue.id, agent.id)
    claimed = store.claim_task(agent.id, "runtime-1")
    assert claimed is not None
    store.start_task(claimed.id)
    completed = store.complete_task(claimed.id, {"summary": "done"})
    assert completed.status.value == "completed"
    assert completed.result == {"summary": "done"}
    assert completed.completed_at is not None


def test_get_pending_member_tasks_returns_only_non_terminal(store: Store):
    leader = store.create_agent("Leader", "", ["codex"], [])
    member = store.create_agent("Member", "", ["codex"], [])
    squad = store.create_squad("S", leader.id)

    issue = store.create_issue("t", "", AssigneeType.SQUAD, squad.id)

    # A completed member task — should NOT appear in pending.
    t_done = store.enqueue_task(issue.id, member.id, squad_id=squad.id)
    store.claim_task(member.id, "rt-done")
    store.start_task(t_done.id)
    store.complete_task(t_done.id, {})

    # A queued member task — should appear.
    t_pending = store.enqueue_task(issue.id, member.id, squad_id=squad.id)

    pending = store.get_pending_member_tasks(squad.id)
    ids = {t.id for t in pending}
    assert t_pending.id in ids
    assert t_done.id not in ids
    for t in pending:
        assert t.status.value not in ("completed", "failed", "cancelled")
