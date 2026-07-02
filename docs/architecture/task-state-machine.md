# Task State Machine

> Derives from: multica migration 001 (`agent_task_queue`), migration 055 (lease/retry)
> Source: `server/migrations/001_init.up.sql`, `server/migrations/055_task_lease_and_retry.up.sql`

## Purpose

Durable task lifecycle management. A task is one execution attempt of an issue
by an agent. The state machine guarantees: atomic claim (no double-execution),
one active task per issue, runtime/profile capacity limits, explicit failure
classification, retry chain via `parent_task_id`.

## States

```
                    ┌──────────┐
         create     │  queued  │
              ──────►          │
                    └────┬─────┘
                         │ claim (atomic)
                         ▼
                    ┌──────────┐
                    │ claimed  │
                    │          │
                    └────┬─────┘
                         │ start execution
                         ▼
                    ┌──────────┐  cancel    ┌───────────┐
                    │ running  │───────────►│ cancelled │
                    │          │            └───────────┘
                    └──┬───┬───┘
               complete│   │fail
                       ▼   ▼
              ┌────────┐  ┌────────┐
              │completed│ │ failed │
              └────────┘  └───┬────┘
                              │ retry? (attempt < max_attempts)
                              ▼
                         ┌──────────┐
                         │  queued  │ (new task, parent_task_id = this)
                         └──────────┘
```

## Legal Transitions

| From | To | Trigger | Actor |
|------|----|---------|-------|
| queued | claimed | `claim_task(runtime_id)` | daemon |
| queued | preparing | `claim_taskrun_for_runtime_machine(runtime_id)` | runtime daemon |
| queued | cancelled | `cancel_task(task_id)` | user/API |
| preparing | running | `start_taskrun(taskrun_id)` | runtime daemon |
| preparing | failed | `fail_task(task_id, error, reason)` | runtime daemon |
| preparing | cancelled | `cancel_task(task_id)` | user/API |
| claimed | running | `start_task(task_id)` | daemon |
| claimed | queued | claim timeout / daemon restart | daemon recovery |
| claimed | cancelled | `cancel_task(task_id)` | user/API |
| running | completed | `complete_task(task_id, result)` | daemon |
| running | failed | `fail_task(task_id, error, reason)` | daemon |
| running | cancelled | `cancel_task(task_id)` | user/API |

**Illegal transitions are rejected with `InvalidStateTransition` error. No silent recovery.**
Terminal task states (`completed`, `failed`, `cancelled`) cannot be cancelled.

Retry does not mutate a failed task back to `queued`. `retry_task(task_id)`
creates a new queued task with `parent_task_id` set to the failed task.

## Failure Classification

> Derives from: multica migration 055 `failure_reason` column

| Reason | Meaning | Retry? | Backoff |
|--------|---------|--------|---------|
| `agent_error` | Agent CLI exited non-zero or produced invalid output | yes (≤ max_attempts) | immediate |
| `timeout` | Execution exceeded `timeout_seconds` | yes | 10s delay |
| `runtime_offline` | Daemon heartbeat lost / runtime unreachable | yes (wait for recovery) | 30s delay |
| `runtime_recovery` | Daemon restarted mid-execution, task was running | yes | immediate |
| `manual` | User explicitly cancelled or force-failed | no | — |

## Task Model

```python
class TaskStatus(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class FailureReason(str, Enum):
    AGENT_ERROR = "agent_error"
    TIMEOUT = "timeout"
    RUNTIME_OFFLINE = "runtime_offline"
    RUNTIME_RECOVERY = "runtime_recovery"
    MANUAL = "manual"

class Task(BaseModel):
    id: str
    issue_id: str
    agent_id: str          # which agent executes this task
    squad_id: str | None   # set when this is a leader task
    status: TaskStatus
    attempt: int = 1
    max_attempts: int = 2
    parent_task_id: str | None  # retry chain
    failure_reason: FailureReason | None
    dispatched_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    result: dict | None
    error: str | None
    runtime_id: str | None  # which daemon claimed it
```

## Issue Model

> Derives from: multica migration 001 `issue` table (simplified)

```python
class IssueStatus(str, Enum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Issue(BaseModel):
    id: str
    title: str
    description: str
    status: IssueStatus
    assignee_type: AssigneeType  # agent | squad
    assignee_id: str
    created_at: datetime
```

Issue status transitions are decoupled from task status. An issue goes
`todo → in_progress` when its first task is claimed, `in_progress → done`
when a task completes successfully, and `in_progress → failed` when terminal
member taskruns fail without successful work. Manual status changes are always
allowed. Service-level issue cancellation refuses terminal issues (`done`,
`failed`, `cancelled`) instead of rewriting them.

## SQLite Schema

```sql
CREATE TABLE issue (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'backlog'
        CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'failed', 'cancelled')),
    assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
    assignee_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE task (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    squad_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'preparing', 'claimed', 'running',
                          'completed', 'failed', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
    failure_reason TEXT
        CHECK (failure_reason IS NULL OR failure_reason IN
               ('agent_error', 'timeout', 'runtime_offline', 'runtime_recovery',
                'manual', 'policy_blocked', 'provider_error', 'test_failure',
                'routing_failure', 'llm_parse_failure')),
    dispatched_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,  -- JSON
    error TEXT,
    runtime_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Atomic claim: only one daemon can claim a queued task
CREATE INDEX idx_task_claim ON task(status, created_at) WHERE status = 'queued';

-- Issue serialization: only one active task may edit an issue at a time
CREATE UNIQUE INDEX idx_task_one_active_per_issue
    ON task(issue_id)
    WHERE status IN ('claimed', 'preparing', 'running');
```

## Atomic Claim Contract

```sql
-- claim_task must use this pattern (BEGIN IMMEDIATE for write lock):
BEGIN IMMEDIATE;
  UPDATE task SET status = 'claimed', runtime_id = ?, dispatched_at = datetime('now')
  WHERE id = (
    SELECT id FROM task WHERE status = 'queued' AND agent_id = ?
      AND NOT EXISTS (
        SELECT 1 FROM task AS active
        WHERE active.issue_id = task.issue_id
          AND active.status IN ('claimed', 'preparing', 'running')
      )
    ORDER BY created_at LIMIT 1
  )
  RETURNING *;
COMMIT;
```

If two daemons race, `BEGIN IMMEDIATE` serializes them. The active-per-issue
partial unique index is the invariant backstop; claim queries also skip issues
that already have an active sibling. RuntimeMachine claim additionally checks
the active RuntimeLease count against `runtime_machine.max_concurrent_taskruns`
and the active task count for an AgentProfile against
`agent_profile.max_concurrent_taskruns`.

## Extension Points

- **New failure reason**: add to `FailureReason` enum + CHECK constraint migration + retry policy table
- **New backend type**: implement `ExecutionBackend` protocol (see harness-backend.md)
- **New squad member role**: add to `squad_member.role` — no schema change needed

## Tests Required

| Test | What it verifies |
|------|-----------------|
| `test_legal_transitions` | Every legal transition succeeds and updates timestamps |
| `test_illegal_transitions` | Every illegal transition raises `InvalidStateTransition` |
| `test_atomic_claim` | Two concurrent claims on same task → only one succeeds |
| `test_claim_task_serializes_active_tasks_per_issue` | Same issue cannot have two active tasks |
| `test_runtime_machine_claim_respects_runtime_capacity` | RuntimeMachine capacity blocks excess claims |
| `test_runtime_machine_claim_respects_agent_profile_capacity` | AgentProfile capacity blocks excess claims |
| `test_retry_creates_new_task` | `retry_task` creates new task with `parent_task_id` set, `attempt` incremented |
| `test_max_attempts_exhausted` | After `max_attempts` failures, no auto-retry, `failure_reason` set |
| `test_failure_classification` | Each `FailureReason` maps to correct retry behavior |
| `test_stale_claim_recovery` | `claimed` task with no heartbeat for N seconds → back to `queued` |
