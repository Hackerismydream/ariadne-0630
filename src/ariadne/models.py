"""Ariadne domain models.

All Pydantic BaseModel + Enum definitions that other modules depend on.
No SQLite, no logic — just the type definitions.

Field definitions follow:
- docs/architecture/task-state-machine.md (Task, Issue, TaskStatus, FailureReason, IssueStatus)
- docs/architecture/squad-orchestration.md (Squad, SquadMember, SquadBriefing, RosterEntry,
  DelegationDecision)
- docs/architecture/harness-backend.md (ExecutionContext, ExecutionResult, ProgressUpdate)
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Lifecycle state of a Task (atomic-claim state machine)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IssueStatus(str, Enum):
    """Lifecycle state of an Issue (decoupled from Task status)."""

    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class FailureReason(str, Enum):
    """Why a Task failed. Drives retry policy + backoff."""

    AGENT_ERROR = "agent_error"
    TIMEOUT = "timeout"
    RUNTIME_OFFLINE = "runtime_offline"
    RUNTIME_RECOVERY = "runtime_recovery"
    MANUAL = "manual"


class AssigneeType(str, Enum):
    """Whether an Issue is assigned to a single agent or a squad."""

    AGENT = "agent"
    SQUAD = "squad"


# ---------------------------------------------------------------------------
# Issue / Task
# ---------------------------------------------------------------------------


class Issue(BaseModel):
    """A unit of work. Simplified from multica migration 001 `issue` table."""

    id: str
    title: str
    description: str
    status: IssueStatus
    assignee_type: AssigneeType
    assignee_id: str
    created_at: datetime


class Task(BaseModel):
    """One execution attempt of an issue by an agent.

    Retry chain via `parent_task_id`. Atomic claim guarantees no double-execution.
    """

    id: str
    issue_id: str
    agent_id: str
    squad_id: str | None = None
    status: TaskStatus
    attempt: int = 1
    max_attempts: int = 2
    parent_task_id: str | None = None
    failure_reason: FailureReason | None = None
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict | None = None
    error: str | None = None
    runtime_id: str | None = None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        """True when the task is in a state that cannot transition further."""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


# ---------------------------------------------------------------------------
# Agent / Squad
# ---------------------------------------------------------------------------


class Agent(BaseModel):
    """An executor definition: instructions + capabilities."""

    id: str
    name: str
    instructions: str
    backends: list[str]
    skills: list[str]


class Squad(BaseModel):
    """A coordinated group with one leader agent. Derives from multica migration 084."""

    id: str
    name: str
    leader_id: str
    instructions: str = ""


class SquadMember(BaseModel):
    """Membership link between a squad and an agent (humans not supported in v1)."""

    squad_id: str
    member_type: str
    member_id: str
    role: str


class RosterEntry(BaseModel):
    """One member's capabilities, surfaced to the leader in a briefing."""

    agent_id: str
    name: str
    role: str
    skills: list[str]
    backends: list[str]


class SquadBriefing(BaseModel):
    """The 3-section briefing injected into a leader task.

    Mirrors multica's `buildSquadLeaderBriefing` output, structured for Python
    rather than markdown.
    """

    protocol: str
    roster: list[RosterEntry]
    instructions: str


class DelegationDecision(BaseModel):
    """Output of the leader agent's delegation reasoning.

    Structured (not @mention) so it is testable, replayable, and validates
    against the roster.
    """

    target_agent_id: str
    backend: str
    handoff_prompt: str
    reason: str
    skill_refs: list[str]


# ---------------------------------------------------------------------------
# Harness backend
# ---------------------------------------------------------------------------


class ExecutionContext(BaseModel):
    """Input to an ExecutionBackend. Backend knows nothing about squads/issues."""

    task_id: str
    agent_name: str
    agent_instructions: str
    handoff_prompt: str
    target_repo_path: str
    skill_refs: list[str]
    timeout_seconds: int = 600
    confirm_execution: bool = False
    model: str | None = None
    effort: str | None = None


class ExecutionResult(BaseModel):
    """Output of an ExecutionBackend."""

    backend_name: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    diff: str | None = None
    changed_files: list[str]
    test_result: str | None = None
    failure_reason: FailureReason | None = None
    duration_seconds: float
    command: str


class ProgressUpdate(BaseModel):
    """In-flight progress reported by a backend via callback. Derives from multica ReportProgress."""

    task_id: str
    summary: str
    step: int
    total: int
    timestamp: datetime
