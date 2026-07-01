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

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Lifecycle state of a Task (atomic-claim state machine)."""

    QUEUED = "queued"
    PREPARING = "preparing"
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


class RuntimeMachineStatus(str, Enum):
    """Lifecycle state of a local execution host."""

    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"
    DISABLED = "disabled"


class RuntimeCapabilityStatus(str, Enum):
    """Health state of an executable coding-agent capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class RuntimeLeaseStatus(str, Enum):
    """Temporary ownership state for a TaskRun."""

    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AgentProfileStatus(str, Enum):
    """Lifecycle state of a durable teammate profile."""

    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class LeaderDecisionOutcome(str, Enum):
    """Outcome of one squad leader activation."""

    ACTION = "action"
    NO_ACTION = "no_action"
    FAILED = "failed"
    DONE = "done"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class RuntimeMachine(BaseModel):
    """A durable execution host, usually one local daemon instance."""

    id: str
    name: str
    status: RuntimeMachineStatus
    version: str = ""
    device_info: dict = Field(default_factory=dict)
    last_heartbeat_at: datetime | None = None
    max_concurrent_taskruns: int = 1
    workspace_root: str = ""
    repo_allowlist: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class RuntimeCapability(BaseModel):
    """An executable coding-agent capability exposed by a RuntimeMachine."""

    id: str
    runtime_machine_id: str
    provider: str
    command_path: str = ""
    version: str = ""
    models: list[str] = Field(default_factory=list)
    status: RuntimeCapabilityStatus
    health_error: str | None = None
    default_args: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    last_checked_at: datetime | None = None


class RuntimeLease(BaseModel):
    """Temporary ownership of a TaskRun by a RuntimeMachine."""

    id: str
    taskrun_id: str
    runtime_machine_id: str
    runtime_capability_id: str
    status: RuntimeLeaseStatus
    lease_token: str
    acquired_at: datetime
    last_heartbeat_at: datetime | None = None
    released_at: datetime | None = None
    expires_at: datetime
    revoke_reason: str | None = None
    metadata: dict = Field(default_factory=dict)


class IssueTimelineEvent(BaseModel):
    """Issue-level product history event."""

    id: str
    issue_id: str
    event_type: str
    actor_type: str
    actor_id: str | None = None
    taskrun_id: str | None = None
    runtime_lease_id: str | None = None
    leader_decision_id: str | None = None
    comment_id: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


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
    handoff_prompt: str | None = None
    trace_id: str | None = None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        """True when the task is in a state that cannot transition further."""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


class TaskRun(Task):
    """v1 name for one execution attempt against an Issue.

    Task remains as a compatibility model for the original public surface.
    """

    @property
    def agent_profile_id(self) -> str:
        return self.agent_id

    @property
    def parent_taskrun_id(self) -> str | None:
        return self.parent_task_id


class TaskRunClaim(BaseModel):
    """Result of atomically claiming a TaskRun through a RuntimeLease."""

    taskrun: TaskRun
    lease: RuntimeLease


# ---------------------------------------------------------------------------
# AgentProfile / Skill / Squad
# ---------------------------------------------------------------------------


class Skill(BaseModel):
    """First-class routing and execution guidance bindable to AgentProfiles."""

    id: str
    name: str
    description: str = ""
    when_to_use: str = ""
    prompt_snippet: str = ""
    tools_allowed: list[str] = Field(default_factory=list)
    test_command: str | None = None
    source_path: str | None = None
    version: str = ""
    created_at: datetime
    updated_at: datetime


class AgentProfile(BaseModel):
    """Durable teammate identity and routing policy."""

    id: str
    name: str
    description: str = ""
    instructions: str = ""
    preferred_capabilities: list[str] = Field(default_factory=list)
    runtime_policy: dict = Field(default_factory=dict)
    max_concurrent_taskruns: int = 1
    status: AgentProfileStatus = AgentProfileStatus.ACTIVE
    created_at: datetime
    updated_at: datetime


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


class LeaderDecision(BaseModel):
    """Replayable product fact produced by one squad leader turn."""

    outcome: LeaderDecisionOutcome
    reason: str = ""
    delegation_payload: dict = Field(default_factory=dict)
    created_taskrun_ids: list[str] = Field(default_factory=list)
    id: str | None = None
    issue_id: str | None = None
    squad_id: str | None = None
    leader_task_id: str | None = None
    created_at: datetime | None = None


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
    trace_id: str | None = None


class ExecutionResult(BaseModel):
    """Output of an ExecutionBackend."""

    backend_name: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    diff: str | None = None
    changed_files: list[str]
    failure_reason: FailureReason | None = None
    duration_seconds: float
    command: str
    metadata: dict | None = None


class ProgressUpdate(BaseModel):
    """In-flight progress reported by a backend via callback. Derives from multica ReportProgress."""

    task_id: str
    summary: str
    step: int
    total: int
    timestamp: datetime
