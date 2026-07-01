"""Tests for ariadne.models.

Covers every requirement in docs/plan/tasks/core-001.md:
- Enum values match design docs exactly + member counts
- Task required-field enforcement + defaults
- DelegationDecision requires all 5 fields (no defaults)
- ExecutionResult.success=True has no failure_reason
- JSON round-trip for all models
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ariadne.models import (
    Agent,
    AssigneeType,
    DelegationDecision,
    ExecutionContext,
    ExecutionResult,
    FailureReason,
    Issue,
    IssueStatus,
    ProgressUpdate,
    RosterEntry,
    Squad,
    SquadBriefing,
    SquadMember,
    Task,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Enum values + member counts
# ---------------------------------------------------------------------------


def test_task_status_values():
    assert TaskStatus.QUEUED.value == "queued"
    assert TaskStatus.PREPARING.value == "preparing"
    assert TaskStatus.CLAIMED.value == "claimed"
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.FAILED.value == "failed"
    assert TaskStatus.CANCELLED.value == "cancelled"


def test_task_status_has_exactly_seven_members():
    assert len(list(TaskStatus)) == 7
    assert {m.value for m in TaskStatus} == {
        "queued",
        "preparing",
        "claimed",
        "running",
        "completed",
        "failed",
        "cancelled",
    }


def test_issue_status_values():
    assert IssueStatus.BACKLOG.value == "backlog"
    assert IssueStatus.TODO.value == "todo"
    assert IssueStatus.IN_PROGRESS.value == "in_progress"
    assert IssueStatus.DONE.value == "done"
    assert IssueStatus.CANCELLED.value == "cancelled"


def test_failure_reason_values():
    assert FailureReason.AGENT_ERROR.value == "agent_error"
    assert FailureReason.TIMEOUT.value == "timeout"
    assert FailureReason.RUNTIME_OFFLINE.value == "runtime_offline"
    assert FailureReason.RUNTIME_RECOVERY.value == "runtime_recovery"
    assert FailureReason.MANUAL.value == "manual"
    assert FailureReason.POLICY_BLOCKED.value == "policy_blocked"
    assert FailureReason.PROVIDER_ERROR.value == "provider_error"
    assert FailureReason.TEST_FAILURE.value == "test_failure"
    assert FailureReason.ROUTING_FAILURE.value == "routing_failure"
    assert FailureReason.LLM_PARSE_FAILURE.value == "llm_parse_failure"


def test_failure_reason_has_exactly_ten_members():
    assert len(list(FailureReason)) == 10
    assert {m.value for m in FailureReason} == {
        "agent_error",
        "timeout",
        "runtime_offline",
        "runtime_recovery",
        "manual",
        "policy_blocked",
        "provider_error",
        "test_failure",
        "routing_failure",
        "llm_parse_failure",
    }


def test_assignee_type_values():
    assert AssigneeType.AGENT.value == "agent"
    assert AssigneeType.SQUAD.value == "squad"
    assert len(list(AssigneeType)) == 2


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _task_kwargs(**overrides):
    kwargs = {
        "id": "task-1",
        "issue_id": "issue-1",
        "agent_id": "agent-1",
        "status": TaskStatus.QUEUED,
        "created_at": _now(),
    }
    kwargs.update(overrides)
    return kwargs


def test_task_accepts_all_required_fields():
    task = Task(**_task_kwargs())
    assert task.id == "task-1"
    assert task.issue_id == "issue-1"
    assert task.agent_id == "agent-1"
    assert task.status == TaskStatus.QUEUED
    assert task.created_at == _now()


def test_task_rejects_missing_required_fields():
    required = ["id", "issue_id", "agent_id", "status", "created_at"]
    for field in required:
        kwargs = _task_kwargs()
        kwargs.pop(field)
        with pytest.raises(ValidationError):
            Task(**kwargs)


def test_task_attempt_defaults_to_one():
    task = Task(**_task_kwargs())
    assert task.attempt == 1


def test_task_max_attempts_defaults_to_two():
    task = Task(**_task_kwargs())
    assert task.max_attempts == 2


def test_task_optional_fields_default_none():
    task = Task(**_task_kwargs())
    assert task.squad_id is None
    assert task.parent_task_id is None
    assert task.failure_reason is None
    assert task.dispatched_at is None
    assert task.started_at is None
    assert task.completed_at is None
    assert task.result is None
    assert task.error is None
    assert task.runtime_id is None


def test_task_is_terminal_property():
    assert Task(**_task_kwargs(status=TaskStatus.COMPLETED)).is_terminal is True
    assert Task(**_task_kwargs(status=TaskStatus.FAILED)).is_terminal is True
    assert Task(**_task_kwargs(status=TaskStatus.CANCELLED)).is_terminal is True
    assert Task(**_task_kwargs(status=TaskStatus.QUEUED)).is_terminal is False
    assert Task(**_task_kwargs(status=TaskStatus.CLAIMED)).is_terminal is False
    assert Task(**_task_kwargs(status=TaskStatus.RUNNING)).is_terminal is False


def test_task_accepts_failure_reason_enum():
    task = Task(**_task_kwargs(failure_reason=FailureReason.TIMEOUT))
    assert task.failure_reason == FailureReason.TIMEOUT


# ---------------------------------------------------------------------------
# Issue model
# ---------------------------------------------------------------------------


def test_issue_round_trip():
    issue = Issue(
        id="issue-1",
        title="Fix bug",
        description="desc",
        status=IssueStatus.TODO,
        assignee_type=AssigneeType.AGENT,
        assignee_id="agent-1",
        created_at=_now(),
    )
    data = issue.model_dump_json()
    restored = Issue.model_validate_json(data)
    assert restored == issue


# ---------------------------------------------------------------------------
# DelegationDecision — all 5 fields required, no defaults
# ---------------------------------------------------------------------------


def _delegation_kwargs():
    return {
        "target_agent_id": "agent-2",
        "backend": "codex",
        "handoff_prompt": "do the thing",
        "reason": "best skills match",
        "skill_refs": ["python", "testing"],
    }


def test_delegation_decision_accepts_all_fields():
    d = DelegationDecision(**_delegation_kwargs())
    assert d.target_agent_id == "agent-2"
    assert d.backend == "codex"
    assert d.handoff_prompt == "do the thing"
    assert d.reason == "best skills match"
    assert d.skill_refs == ["python", "testing"]


def test_delegation_decision_requires_all_five_fields():
    for field in _delegation_kwargs():
        kwargs = _delegation_kwargs()
        kwargs.pop(field)
        with pytest.raises(ValidationError):
            DelegationDecision(**kwargs)


def test_delegation_decision_has_no_defaults():
    d = DelegationDecision(**_delegation_kwargs())
    # All fields are non-optional; every one was explicitly provided.
    assert d.model_fields_set == {
        "target_agent_id",
        "backend",
        "handoff_prompt",
        "reason",
        "skill_refs",
    }


# ---------------------------------------------------------------------------
# ExecutionResult — success=True has no failure_reason
# ---------------------------------------------------------------------------


def _result_kwargs(**overrides):
    kwargs = {
        "backend_name": "codex",
        "success": True,
        "exit_code": 0,
        "stdout": "done",
        "stderr": "",
        "changed_files": [],
        "duration_seconds": 1.5,
        "command": "codex exec",
    }
    kwargs.update(overrides)
    return kwargs


def test_execution_result_success_has_no_failure_reason():
    result = ExecutionResult(**_result_kwargs(success=True))
    assert result.success is True
    assert result.failure_reason is None


def test_execution_result_failure_may_have_reason():
    result = ExecutionResult(
        **_result_kwargs(success=False, failure_reason=FailureReason.TIMEOUT)
    )
    assert result.success is False
    assert result.failure_reason == FailureReason.TIMEOUT


def test_execution_result_required_fields_enforced():
    required = [
        "backend_name",
        "success",
        "exit_code",
        "stdout",
        "stderr",
        "changed_files",
        "duration_seconds",
        "command",
    ]
    for field in required:
        kwargs = _result_kwargs()
        kwargs.pop(field)
        with pytest.raises(ValidationError):
            ExecutionResult(**kwargs)


# ---------------------------------------------------------------------------
# JSON round-trip for every model
# ---------------------------------------------------------------------------


def test_task_round_trip():
    task = Task(
        **_task_kwargs(
            squad_id="squad-1",
            failure_reason=FailureReason.AGENT_ERROR,
            dispatched_at=_now(),
            result={"k": "v"},
        )
    )
    restored = Task.model_validate_json(task.model_dump_json())
    assert restored == task
    assert restored.failure_reason == FailureReason.AGENT_ERROR


def test_agent_round_trip():
    agent = Agent(
        id="agent-1",
        name="Coder",
        instructions="be useful",
        backends=["codex", "claude-code"],
        skills=["python"],
    )
    restored = Agent.model_validate_json(agent.model_dump_json())
    assert restored == agent


def test_squad_round_trip():
    squad = Squad(id="squad-1", name="Alpha", leader_id="agent-1", instructions="go")
    restored = Squad.model_validate_json(squad.model_dump_json())
    assert restored == squad
    # instructions defaults to ""
    assert Squad(id="squad-1", name="Alpha", leader_id="agent-1").instructions == ""


def test_squad_member_round_trip():
    member = SquadMember(
        squad_id="squad-1", member_type="agent", member_id="agent-2", role="coder"
    )
    restored = SquadMember.model_validate_json(member.model_dump_json())
    assert restored == member


def test_roster_entry_round_trip():
    entry = RosterEntry(
        agent_id="agent-2",
        name="Coder",
        role="coder",
        skills=["python"],
        backends=["codex"],
    )
    restored = RosterEntry.model_validate_json(entry.model_dump_json())
    assert restored == entry


def test_squad_briefing_round_trip():
    briefing = SquadBriefing(
        protocol="coordinate, don't implement",
        roster=[
            RosterEntry(
                agent_id="agent-2",
                name="Coder",
                role="coder",
                skills=["python"],
                backends=["codex"],
            )
        ],
        instructions="ship it",
    )
    restored = SquadBriefing.model_validate_json(briefing.model_dump_json())
    assert restored == briefing
    assert restored.roster[0].agent_id == "agent-2"


def test_delegation_decision_round_trip():
    d = DelegationDecision(**_delegation_kwargs())
    restored = DelegationDecision.model_validate_json(d.model_dump_json())
    assert restored == d


def test_execution_context_round_trip():
    ctx = ExecutionContext(
        task_id="task-1",
        agent_name="Coder",
        agent_instructions="be useful",
        handoff_prompt="do the thing",
        target_repo_path="/tmp/repo",
        skill_refs=["python"],
    )
    restored = ExecutionContext.model_validate_json(ctx.model_dump_json())
    assert restored == ctx
    # defaults
    assert ctx.timeout_seconds == 600
    assert ctx.confirm_execution is False
    assert ctx.model is None
    assert ctx.effort is None


def test_execution_result_round_trip():
    result = ExecutionResult(**_result_kwargs(success=False, diff="@@"))
    restored = ExecutionResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_progress_update_round_trip():
    update = ProgressUpdate(
        task_id="task-1",
        summary="halfway",
        step=2,
        total=4,
        timestamp=_now(),
    )
    data = update.model_dump_json()
    restored = ProgressUpdate.model_validate_json(data)
    assert restored == update
    # timestamp survives ISO round-trip
    assert restored.timestamp == update.timestamp


def test_datetime_serializes_iso():
    """All datetime fields use ISO format in JSON."""
    task = Task(**_task_kwargs(created_at=_now()))
    raw = task.model_dump_json()
    assert "2026-06-30T12:00:00" in raw
