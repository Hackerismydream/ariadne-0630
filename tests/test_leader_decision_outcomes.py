"""LeaderDecision outcome records and replayable squad facts."""

import pytest

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import (
    AssigneeType,
    DelegationDecision,
    FailureReason,
    IssueStatus,
    LeaderDecision,
    LeaderDecisionOutcome,
    TaskStatus,
)
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def squad_setup(store):
    leader = store.create_agent("Leader", "Coordinate", ["dry-run"], [])
    member = store.create_agent("Coder", "Write code", ["dry-run"], ["python"])
    squad = store.create_squad("Alpha", leader.id, instructions="ship it")
    store.add_squad_member(squad.id, member.id, role="coder")
    issue = store.create_issue("Build feature", "Do the thing", AssigneeType.SQUAD, squad.id)
    leader_task = store.enqueue_task(issue.id, leader.id, squad_id=squad.id)
    return squad, leader, member, issue, leader_task


def fail_member_attempt(store, issue, squad, member, error):
    task = store.enqueue_task(issue.id, member.id, squad_id=squad.id)
    claimed = store.claim_task(member.id, f"rt-{task.id}")
    assert claimed is not None
    assert claimed.id == task.id
    store.start_task(claimed.id)
    return store.fail_task(claimed.id, error, FailureReason.TIMEOUT)


def complete_member_attempt(store, issue, squad, member, result):
    task = store.enqueue_task(issue.id, member.id, squad_id=squad.id)
    claimed = store.claim_task(member.id, f"rt-{task.id}")
    assert claimed is not None
    assert claimed.id == task.id
    store.start_task(claimed.id)
    return store.complete_task(claimed.id, result)


def complete_leader_action_then_fail_member_attempts(store, squad, leader, member, issue, leader_task):
    decision = DelegationDecision(
        target_agent_id=member.id,
        backend="dry-run",
        handoff_prompt="Implement it.",
        reason="delegate implementation",
        skill_refs=[],
    )
    store.claim_task(leader.id, "rt-leader-action")
    Orchestrator(store=store, llm_decide=lambda b, i, cr=None: decision).handle_leader_task(
        leader_task
    )

    action = store.list_leader_decisions(issue.id)[0]
    first_member_task_id = action.created_taskrun_ids[0]
    claimed = store.claim_task(member.id, "rt-member-1")
    assert claimed is not None
    assert claimed.id == first_member_task_id
    store.start_task(claimed.id)
    failed = store.fail_task(claimed.id, "attempt 1 timed out", FailureReason.TIMEOUT)

    retry = store.retry_taskrun(failed.id)
    claimed_retry = store.claim_task(member.id, "rt-member-2")
    assert claimed_retry is not None
    assert claimed_retry.id == retry.id
    store.start_task(claimed_retry.id)
    failed_retry = store.fail_task(
        claimed_retry.id,
        "attempt 2 timed out",
        FailureReason.TIMEOUT,
    )

    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: None)
    orc.on_member_task_complete(failed_retry)
    leader_reeval = store.claim_task(leader.id, "rt-leader-reeval")
    assert leader_reeval is not None
    return leader_reeval


def test_action_records_leader_decision_and_child_taskrun(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    store.claim_task(leader.id, "rt-1")

    decision = DelegationDecision(
        target_agent_id=member.id,
        backend="dry-run",
        handoff_prompt="Implement it.",
        reason="best available coder",
        skill_refs=["python"],
    )
    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: decision)
    orc.handle_leader_task(leader_task)

    records = store.list_leader_decisions(issue.id)
    assert len(records) == 1
    record = records[0]
    assert record.outcome == LeaderDecisionOutcome.ACTION
    assert record.reason == "best available coder"
    assert record.delegation_payload["target_agent_id"] == member.id
    assert len(record.created_taskrun_ids) == 1
    assert record.created_taskrun_ids[0].startswith("taskrun-")

    child = store.get_taskrun(record.created_taskrun_ids[0])
    assert child is not None
    assert child.agent_profile_id == member.id
    assert child.handoff_prompt == "Implement it."

    timeline = store.get_issue_timeline(issue.id)
    leader_events = [event for event in timeline if event.event_type == "leader_decided"]
    assert len(leader_events) == 1
    assert leader_events[0].leader_decision_id == record.id
    assert leader_events[0].payload["outcome"] == "action"
    assert leader_events[0].payload["created_taskrun_ids"] == record.created_taskrun_ids


def test_no_action_keeps_issue_open_with_observable_reason(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    store.claim_task(leader.id, "rt-1")

    no_action = LeaderDecision(
        outcome=LeaderDecisionOutcome.NO_ACTION,
        reason="Waiting for product clarification.",
    )
    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: no_action)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.BACKLOG
    assert store.get_task(leader_task.id).status == TaskStatus.COMPLETED
    record = store.list_leader_decisions(issue.id)[0]
    assert record.outcome == LeaderDecisionOutcome.NO_ACTION
    assert record.reason == "Waiting for product clarification."
    assert record.created_taskrun_ids == []


def test_failed_records_coordination_failure_without_closing_issue(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    store.claim_task(leader.id, "rt-1")

    failed = LeaderDecision(
        outcome=LeaderDecisionOutcome.FAILED,
        reason="No member has the required capability.",
    )
    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: failed)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.BACKLOG
    assert store.get_task(leader_task.id).status == TaskStatus.FAILED
    record = store.list_leader_decisions(issue.id)[0]
    assert record.outcome == LeaderDecisionOutcome.FAILED
    assert "required capability" in record.reason


def test_done_records_decision_then_closes_issue(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    store.claim_task(leader.id, "rt-1")

    done = LeaderDecision(
        outcome=LeaderDecisionOutcome.DONE,
        reason="Current timeline shows implementation and tests are complete.",
    )
    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: done)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.DONE
    assert store.get_task(leader_task.id).status == TaskStatus.COMPLETED
    record = store.list_leader_decisions(issue.id)[0]
    assert record.outcome == LeaderDecisionOutcome.DONE
    assert record.delegation_payload["issue_timeline_event_count"] >= 1
    event_types = [event.event_type for event in store.get_issue_timeline(issue.id)]
    assert event_types.index("leader_decided") < event_types.index("issue_closed")


def test_squad_failed_member_attempts_do_not_close_issue_done(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    fail_member_attempt(store, issue, squad, member, "attempt 1 timed out")
    fail_member_attempt(store, issue, squad, member, "attempt 2 timed out")
    store.claim_task(leader.id, "rt-leader")

    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: None)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status != IssueStatus.DONE


def test_squad_failed_member_attempts_mark_issue_failed(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    fail_member_attempt(store, issue, squad, member, "attempt 1 timed out")
    fail_member_attempt(store, issue, squad, member, "attempt 2 timed out")
    store.claim_task(leader.id, "rt-leader")

    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: None)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.FAILED
    assert store.get_task(leader_task.id).status == TaskStatus.FAILED
    record = store.list_leader_decisions(issue.id)[0]
    assert record.outcome == LeaderDecisionOutcome.FAILED
    assert record.reason == "all completed member taskruns failed"


def test_completed_leader_action_does_not_make_failed_members_done(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    leader_reeval = complete_leader_action_then_fail_member_attempts(
        store,
        squad,
        leader,
        member,
        issue,
        leader_task,
    )

    Orchestrator(store=store, llm_decide=lambda b, i, cr=None: None).handle_leader_task(
        leader_reeval
    )

    assert store.get_issue(issue.id).status == IssueStatus.FAILED
    assert [
        decision.outcome for decision in store.list_leader_decisions(issue.id)
    ] == [LeaderDecisionOutcome.ACTION, LeaderDecisionOutcome.FAILED]


def test_default_deterministic_failed_members_mark_issue_failed(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    leader_reeval = complete_leader_action_then_fail_member_attempts(
        store,
        squad,
        leader,
        member,
        issue,
        leader_task,
    )

    Orchestrator(store=store).handle_leader_task(leader_reeval)

    assert store.get_issue(issue.id).status == IssueStatus.FAILED
    assert [
        decision.outcome for decision in store.list_leader_decisions(issue.id)
    ] == [LeaderDecisionOutcome.ACTION, LeaderDecisionOutcome.FAILED]


def test_explicit_failed_decision_after_failed_members_marks_issue_failed(
    store,
    squad_setup,
):
    squad, leader, member, issue, leader_task = squad_setup
    leader_reeval = complete_leader_action_then_fail_member_attempts(
        store,
        squad,
        leader,
        member,
        issue,
        leader_task,
    )

    failed = LeaderDecision(
        outcome=LeaderDecisionOutcome.FAILED,
        reason="All member attempts timed out.",
    )
    Orchestrator(store=store, llm_decide=lambda b, i, cr=None: failed).handle_leader_task(
        leader_reeval
    )

    assert store.get_issue(issue.id).status == IssueStatus.FAILED


def test_squad_one_completed_member_still_allows_done(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    fail_member_attempt(store, issue, squad, member, "first attempt timed out")
    complete_member_attempt(store, issue, squad, member, {"output": "done"})
    store.claim_task(leader.id, "rt-leader")

    orc = Orchestrator(store=store, llm_decide=lambda b, i, cr=None: None)
    orc.handle_leader_task(leader_task)

    assert store.get_issue(issue.id).status == IssueStatus.DONE
    assert store.get_task(leader_task.id).status == TaskStatus.COMPLETED


def test_member_completion_loop_records_action_then_done(store, squad_setup):
    squad, leader, member, issue, leader_task = squad_setup
    call_count = [0]

    def decide(briefing, issue, completed_results=None):
        call_count[0] += 1
        if call_count[0] == 1:
            entry = briefing.roster[0]
            return DelegationDecision(
                target_agent_id=entry.agent_id,
                backend="dry-run",
                handoff_prompt="Implement it.",
                reason="delegate implementation",
                skill_refs=entry.skills,
            )
        assert completed_results
        return LeaderDecision(
            outcome=LeaderDecisionOutcome.DONE,
            reason="Member task completed successfully.",
        )

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        poll_interval=0.001,
        orchestrator=Orchestrator(store=store, llm_decide=decide),
    )
    daemon.start(max_iterations=10)

    assert store.get_issue(issue.id).status == IssueStatus.DONE
    assert call_count[0] >= 2
    assert [
        decision.outcome for decision in store.list_leader_decisions(issue.id)
    ] == [LeaderDecisionOutcome.ACTION, LeaderDecisionOutcome.DONE]
