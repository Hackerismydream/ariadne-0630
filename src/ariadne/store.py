"""SQLite persistence layer with atomic state transitions.

The control plane — all task lifecycle operations go through this module.
No ORM; raw sqlite3 for minimalism and explicit transaction control.

Schema and state machine follow docs/architecture/task-state-machine.md.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from ariadne.models import (
    Agent,
    AssigneeType,
    FailureReason,
    Issue,
    IssueStatus,
    RuntimeCapability,
    RuntimeCapabilityStatus,
    RuntimeMachine,
    RuntimeMachineStatus,
    Squad,
    SquadMember,
    Task,
    TaskRun,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidStateTransition(Exception):
    """Raised when a task state transition is not in the legal transitions table."""

    def __init__(self, current_status: str, attempted_action: str):
        self.current_status = current_status
        self.attempted_action = attempted_action
        super().__init__(
            f"Cannot {attempted_action} from status '{current_status}'"
        )


class MaxAttemptsExhausted(Exception):
    """Raised when retry_task is called but attempt >= max_attempts."""


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------

# (from_status, to_status) pairs that are legal.
_LEGAL_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (TaskStatus.QUEUED, TaskStatus.CLAIMED),
    (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    (TaskStatus.CLAIMED, TaskStatus.QUEUED),       # stale claim recovery
    (TaskStatus.RUNNING, TaskStatus.COMPLETED),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_machine (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('online', 'offline', 'draining', 'disabled')),
    version TEXT NOT NULL DEFAULT '',
    device_info TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at TEXT,
    max_concurrent_taskruns INTEGER NOT NULL DEFAULT 1,
    workspace_root TEXT NOT NULL DEFAULT '',
    repo_allowlist TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_capability (
    id TEXT PRIMARY KEY,
    runtime_machine_id TEXT NOT NULL REFERENCES runtime_machine(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    command_path TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    models_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL
        CHECK (status IN ('available', 'unavailable', 'degraded', 'disabled')),
    health_error TEXT,
    default_args_json TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    last_checked_at TEXT,
    UNIQUE(runtime_machine_id, provider, command_path)
);

CREATE TABLE IF NOT EXISTS issue (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'backlog'
        CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'cancelled')),
    assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
    assignee_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    squad_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
    failure_reason TEXT
        CHECK (failure_reason IS NULL OR failure_reason IN
               ('agent_error', 'timeout', 'runtime_offline', 'runtime_recovery', 'manual')),
    dispatched_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    runtime_id TEXT,
    handoff_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_claim
    ON task(status, created_at) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    task_id TEXT,
    event TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_log(trace_id);

CREATE TABLE IF NOT EXISTS agent (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    backends TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS squad (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    leader_id TEXT NOT NULL REFERENCES agent(id),
    instructions TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS squad_member (
    id TEXT PRIMARY KEY,
    squad_id TEXT NOT NULL REFERENCES squad(id) ON DELETE CASCADE,
    member_type TEXT NOT NULL DEFAULT 'agent',
    member_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    UNIQUE(squad_id, member_type, member_id)
);
"""


class Store:
    """SQLite-backed persistence with atomic state transitions."""

    def __init__(self, db_path: str = "ariadne.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        import threading
        self._lock = threading.Lock()

        # Migration: add handoff_prompt column if not present
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(task)").fetchall()]
        if "handoff_prompt" not in cols:
            self._conn.execute("ALTER TABLE task ADD COLUMN handoff_prompt TEXT")
            self._conn.commit()
        if "trace_id" not in cols:
            self._conn.execute("ALTER TABLE task ADD COLUMN trace_id TEXT")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        result_raw = row["result"]
        return Task(
            id=row["id"],
            issue_id=row["issue_id"],
            agent_id=row["agent_id"],
            squad_id=row["squad_id"],
            status=TaskStatus(row["status"]),
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            parent_task_id=row["parent_task_id"],
            failure_reason=FailureReason(row["failure_reason"])
            if row["failure_reason"]
            else None,
            dispatched_at=datetime.fromisoformat(row["dispatched_at"])
            if row["dispatched_at"]
            else None,
            started_at=datetime.fromisoformat(row["started_at"])
            if row["started_at"]
            else None,
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None,
            result=json.loads(result_raw) if result_raw else None,
            error=row["error"],
            runtime_id=row["runtime_id"],
            handoff_prompt=row["handoff_prompt"] if "handoff_prompt" in row.keys() else None,
            trace_id=row["trace_id"] if "trace_id" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_taskrun(row: sqlite3.Row) -> TaskRun:
        task = Store._row_to_task(row)
        return TaskRun(**task.model_dump())

    @staticmethod
    def _row_to_issue(row: sqlite3.Row) -> Issue:
        return Issue(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=IssueStatus(row["status"]),
            assignee_type=AssigneeType(row["assignee_type"]),
            assignee_id=row["assignee_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_runtime_machine(row: sqlite3.Row) -> RuntimeMachine:
        return RuntimeMachine(
            id=row["id"],
            name=row["name"],
            status=RuntimeMachineStatus(row["status"]),
            version=row["version"],
            device_info=json.loads(row["device_info"]),
            last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"])
            if row["last_heartbeat_at"]
            else None,
            max_concurrent_taskruns=row["max_concurrent_taskruns"],
            workspace_root=row["workspace_root"],
            repo_allowlist=json.loads(row["repo_allowlist"]),
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_runtime_capability(row: sqlite3.Row) -> RuntimeCapability:
        return RuntimeCapability(
            id=row["id"],
            runtime_machine_id=row["runtime_machine_id"],
            provider=row["provider"],
            command_path=row["command_path"],
            version=row["version"],
            models=json.loads(row["models_json"]),
            status=RuntimeCapabilityStatus(row["status"]),
            health_error=row["health_error"],
            default_args=json.loads(row["default_args_json"]),
            metadata=json.loads(row["metadata"]),
            last_checked_at=datetime.fromisoformat(row["last_checked_at"])
            if row["last_checked_at"]
            else None,
        )

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            instructions=row["instructions"],
            backends=json.loads(row["backends"]),
            skills=json.loads(row["skills"]),
        )

    @staticmethod
    def _row_to_squad(row: sqlite3.Row) -> Squad:
        return Squad(
            id=row["id"],
            name=row["name"],
            leader_id=row["leader_id"],
            instructions=row["instructions"],
        )

    @staticmethod
    def _row_to_squad_member(row: sqlite3.Row) -> SquadMember:
        return SquadMember(
            squad_id=row["squad_id"],
            member_type=row["member_type"],
            member_id=row["member_id"],
            role=row["role"],
        )

    def _load_task(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return self._row_to_task(row)

    def _check_transition(
        self, current: TaskStatus, target: TaskStatus, action: str
    ) -> None:
        if (current, target) not in _LEGAL_TRANSITIONS:
            raise InvalidStateTransition(current.value, action)

    # ------------------------------------------------------------------
    # RuntimeMachine / RuntimeCapability
    # ------------------------------------------------------------------

    def register_runtime_machine(
        self,
        runtime_machine_id: str,
        name: str,
        version: str = "",
        workspace_root: str = "",
        max_concurrent_taskruns: int = 1,
        repo_allowlist: list[str] | None = None,
        device_info: dict | None = None,
        metadata: dict | None = None,
    ) -> RuntimeMachine:
        now = _now_iso()
        existing = self.get_runtime_machine(runtime_machine_id)
        created_at = existing.created_at.isoformat() if existing else now
        self._conn.execute(
            """INSERT INTO runtime_machine
               (id, name, status, version, device_info, last_heartbeat_at,
                max_concurrent_taskruns, workspace_root, repo_allowlist,
                metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    version = excluded.version,
                    device_info = excluded.device_info,
                    max_concurrent_taskruns = excluded.max_concurrent_taskruns,
                    workspace_root = excluded.workspace_root,
                    repo_allowlist = excluded.repo_allowlist,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at""",
            (
                runtime_machine_id,
                name,
                RuntimeMachineStatus.ONLINE.value,
                version,
                json.dumps(device_info or {}),
                existing.last_heartbeat_at.isoformat()
                if existing and existing.last_heartbeat_at
                else None,
                max_concurrent_taskruns,
                workspace_root,
                json.dumps(repo_allowlist or []),
                json.dumps(metadata or {}),
                created_at,
                now,
            ),
        )
        self._conn.commit()
        machine = self.get_runtime_machine(runtime_machine_id)
        if machine is None:
            raise KeyError(f"runtime machine not found: {runtime_machine_id}")
        return machine

    def heartbeat_runtime_machine(self, runtime_machine_id: str) -> RuntimeMachine:
        now = _now_iso()
        self._conn.execute(
            """UPDATE runtime_machine
               SET status = ?, last_heartbeat_at = ?, updated_at = ?
               WHERE id = ?""",
            (RuntimeMachineStatus.ONLINE.value, now, now, runtime_machine_id),
        )
        self._conn.commit()
        machine = self.get_runtime_machine(runtime_machine_id)
        if machine is None:
            raise KeyError(f"runtime machine not found: {runtime_machine_id}")
        return machine

    def get_runtime_machine(self, runtime_machine_id: str) -> RuntimeMachine | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_machine WHERE id = ?", (runtime_machine_id,)
        ).fetchone()
        return self._row_to_runtime_machine(row) if row else None

    def list_runtime_machines(self) -> list[RuntimeMachine]:
        rows = self._conn.execute(
            "SELECT * FROM runtime_machine ORDER BY name"
        ).fetchall()
        return [self._row_to_runtime_machine(r) for r in rows]

    def upsert_runtime_capability(
        self,
        runtime_machine_id: str,
        provider: str,
        command_path: str = "",
        version: str = "",
        status: RuntimeCapabilityStatus = RuntimeCapabilityStatus.UNAVAILABLE,
        health_error: str | None = None,
        models: list[str] | None = None,
        default_args: list[str] | None = None,
        metadata: dict | None = None,
    ) -> RuntimeCapability:
        now = _now_iso()
        existing = self._conn.execute(
            """SELECT * FROM runtime_capability
               WHERE runtime_machine_id = ? AND provider = ? AND command_path = ?""",
            (runtime_machine_id, provider, command_path),
        ).fetchone()
        capability_id = existing["id"] if existing else _new_id("cap")
        self._conn.execute(
            """INSERT INTO runtime_capability
               (id, runtime_machine_id, provider, command_path, version,
                models_json, status, health_error, default_args_json, metadata,
                last_checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(runtime_machine_id, provider, command_path) DO UPDATE SET
                    version = excluded.version,
                    models_json = excluded.models_json,
                    status = excluded.status,
                    health_error = excluded.health_error,
                    default_args_json = excluded.default_args_json,
                    metadata = excluded.metadata,
                    last_checked_at = excluded.last_checked_at""",
            (
                capability_id,
                runtime_machine_id,
                provider,
                command_path,
                version,
                json.dumps(models or []),
                status.value,
                health_error,
                json.dumps(default_args or []),
                json.dumps(metadata or {}),
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM runtime_capability WHERE id = ?", (capability_id,)
        ).fetchone()
        return self._row_to_runtime_capability(row)

    def list_runtime_capabilities(
        self, runtime_machine_id: str | None = None
    ) -> list[RuntimeCapability]:
        if runtime_machine_id is None:
            rows = self._conn.execute(
                "SELECT * FROM runtime_capability ORDER BY provider"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM runtime_capability
                   WHERE runtime_machine_id = ?
                   ORDER BY provider""",
                (runtime_machine_id,),
            ).fetchall()
        return [self._row_to_runtime_capability(r) for r in rows]

    def set_runtime_capability_status(
        self,
        capability_id: str,
        status: RuntimeCapabilityStatus,
        health_error: str | None = None,
    ) -> RuntimeCapability:
        now = _now_iso()
        self._conn.execute(
            """UPDATE runtime_capability
               SET status = ?, health_error = ?, last_checked_at = ?
               WHERE id = ?""",
            (status.value, health_error, now, capability_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM runtime_capability WHERE id = ?", (capability_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"runtime capability not found: {capability_id}")
        return self._row_to_runtime_capability(row)

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    def create_issue(
        self,
        title: str,
        description: str,
        assignee_type: AssigneeType,
        assignee_id: str,
    ) -> Issue:
        issue = Issue(
            id=_new_id("issue"),
            title=title,
            description=description,
            status=IssueStatus.BACKLOG,
            assignee_type=assignee_type,
            assignee_id=assignee_id,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO issue (id, title, description, status, assignee_type, assignee_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                issue.id,
                issue.title,
                issue.description,
                issue.status.value,
                issue.assignee_type.value,
                issue.assignee_id,
                issue.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return issue

    def get_issue(self, issue_id: str) -> Issue | None:
        row = self._conn.execute(
            "SELECT * FROM issue WHERE id = ?", (issue_id,)
        ).fetchone()
        return self._row_to_issue(row) if row else None

    def list_issues(self) -> list[Issue]:
        rows = self._conn.execute("SELECT * FROM issue ORDER BY created_at").fetchall()
        return [self._row_to_issue(r) for r in rows]

    def update_issue_status(self, issue_id: str, status: IssueStatus) -> Issue:
        self._conn.execute(
            "UPDATE issue SET status = ? WHERE id = ?",
            (status.value, issue_id),
        )
        self._conn.commit()
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(f"issue not found: {issue_id}")
        return issue

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    def enqueue_task(
        self,
        issue_id: str,
        agent_id: str,
        squad_id: str | None = None,
        handoff_prompt: str | None = None,
        trace_id: str | None = None,
    ) -> Task:
        return self._enqueue_task_record(
            "task",
            issue_id,
            agent_id,
            squad_id=squad_id,
            handoff_prompt=handoff_prompt,
            trace_id=trace_id,
        )

    def enqueue_taskrun(
        self,
        issue_id: str,
        agent_profile_id: str,
        squad_id: str | None = None,
        handoff_prompt: str | None = None,
        trace_id: str | None = None,
    ) -> TaskRun:
        task = self._enqueue_task_record(
            "taskrun",
            issue_id,
            agent_profile_id,
            squad_id=squad_id,
            handoff_prompt=handoff_prompt,
            trace_id=trace_id,
        )
        return TaskRun(**task.model_dump())

    def _enqueue_task_record(
        self,
        id_prefix: str,
        issue_id: str,
        agent_id: str,
        squad_id: str | None = None,
        handoff_prompt: str | None = None,
        trace_id: str | None = None,
    ) -> Task:
        task = Task(
            id=_new_id(id_prefix),
            issue_id=issue_id,
            agent_id=agent_id,
            squad_id=squad_id,
            status=TaskStatus.QUEUED,
            handoff_prompt=handoff_prompt,
            trace_id=trace_id or _new_id("trace"),
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO task
               (id, issue_id, agent_id, squad_id, status, attempt, max_attempts,
                parent_task_id, failure_reason, handoff_prompt, trace_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id,
                task.issue_id,
                task.agent_id,
                task.squad_id,
                task.status.value,
                task.attempt,
                task.max_attempts,
                task.parent_task_id,
                None,
                task.handoff_prompt,
                task.trace_id,
                task.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        self.log_activity(task.trace_id, task.id, "created", {"status": "queued"})
        return task

    def claim_task(self, agent_id: str, runtime_id: str) -> Task | None:
        """Atomically claim the oldest queued task for the given agent.

        Uses BEGIN IMMEDIATE to serialize concurrent claims — the write
        lock prevents two daemons from claiming the same task.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """SELECT * FROM task
                       WHERE status = 'queued' AND agent_id = ?
                       ORDER BY created_at LIMIT 1""",
                    (agent_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                now = _now_iso()
                self._conn.execute(
                    """UPDATE task
                       SET status = 'claimed', runtime_id = ?, dispatched_at = ?
                       WHERE id = ?""",
                    (runtime_id, now, row["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self._row_to_task(
                self._conn.execute(
                    "SELECT * FROM task WHERE id = ?", (row["id"],)
                ).fetchone()
            )

    def claim_taskrun(self, agent_profile_id: str, runtime_id: str) -> TaskRun | None:
        task = self.claim_task(agent_profile_id, runtime_id)
        return TaskRun(**task.model_dump()) if task else None

    def start_task(self, task_id: str) -> Task:
        task = self._load_task(task_id)
        self._check_transition(task.status, TaskStatus.RUNNING, "start_task")
        now = _now_iso()
        self._conn.execute(
            "UPDATE task SET status = 'running', started_at = ? WHERE id = ?",
            (now, task_id),
        )
        self._conn.commit()
        return self._load_task(task_id)

    def start_taskrun(self, taskrun_id: str) -> TaskRun:
        task = self.start_task(taskrun_id)
        return TaskRun(**task.model_dump())

    def complete_task(self, task_id: str, result: dict) -> Task:
        task = self._load_task(task_id)
        self._check_transition(task.status, TaskStatus.COMPLETED, "complete_task")
        now = _now_iso()
        self._conn.execute(
            """UPDATE task
               SET status = 'completed', result = ?, completed_at = ?
               WHERE id = ?""",
            (json.dumps(result), now, task_id),
        )
        self._conn.commit()
        return self._load_task(task_id)

    def complete_taskrun(self, taskrun_id: str, result: dict) -> TaskRun:
        task = self.complete_task(taskrun_id, result)
        return TaskRun(**task.model_dump())

    def fail_task(self, task_id: str, error: str, reason: FailureReason) -> Task:
        task = self._load_task(task_id)
        self._check_transition(task.status, TaskStatus.FAILED, "fail_task")
        now = _now_iso()
        self._conn.execute(
            """UPDATE task
               SET status = 'failed', error = ?, failure_reason = ?, completed_at = ?
               WHERE id = ?""",
            (error, reason.value, now, task_id),
        )
        self._conn.commit()
        return self._load_task(task_id)

    def fail_taskrun(
        self, taskrun_id: str, error: str, reason: FailureReason
    ) -> TaskRun:
        task = self.fail_task(taskrun_id, error, reason)
        return TaskRun(**task.model_dump())

    def cancel_task(self, task_id: str) -> Task:
        task = self._load_task(task_id)
        # Cancel is only legal from running (per design doc).
        # But we also allow cancelling queued/claimed for user-initiated cancel.
        if task.status not in (TaskStatus.QUEUED, TaskStatus.CLAIMED, TaskStatus.RUNNING):
            raise InvalidStateTransition(task.status.value, "cancel_task")
        now = _now_iso()
        self._conn.execute(
            """UPDATE task
               SET status = 'cancelled', completed_at = ?,
                   failure_reason = 'manual'
               WHERE id = ?""",
            (now, task_id),
        )
        self._conn.commit()
        return self._load_task(task_id)

    def cancel_taskrun(self, taskrun_id: str) -> TaskRun:
        task = self.cancel_task(taskrun_id)
        return TaskRun(**task.model_dump())

    def retry_task(self, task_id: str) -> Task:
        """Create a new queued task that re-attempts the failed one.

        Raises MaxAttemptsExhausted if attempt >= max_attempts.
        """
        old = self._load_task(task_id)
        if old.status != TaskStatus.FAILED:
            raise InvalidStateTransition(old.status.value, "retry_task")
        if old.attempt >= old.max_attempts:
            raise MaxAttemptsExhausted(
                f"task {task_id} already reached max_attempts ({old.max_attempts})"
            )
        new_task = Task(
            id=_new_id("taskrun" if old.id.startswith("taskrun-") else "task"),
            issue_id=old.issue_id,
            agent_id=old.agent_id,
            squad_id=old.squad_id,
            status=TaskStatus.QUEUED,
            attempt=old.attempt + 1,
            max_attempts=old.max_attempts,
            parent_task_id=old.id,
            handoff_prompt=old.handoff_prompt,
            trace_id=old.trace_id,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO task
               (id, issue_id, agent_id, squad_id, status, attempt, max_attempts,
                parent_task_id, failure_reason, handoff_prompt, trace_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_task.id,
                new_task.issue_id,
                new_task.agent_id,
                new_task.squad_id,
                new_task.status.value,
                new_task.attempt,
                new_task.max_attempts,
                new_task.parent_task_id,
                None,
                new_task.handoff_prompt,
                new_task.trace_id,
                new_task.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        self.log_activity(new_task.trace_id, new_task.id, "retried", {"attempt": new_task.attempt, "parent": old.id})
        return new_task

    def retry_taskrun(self, taskrun_id: str) -> TaskRun:
        task = self.retry_task(taskrun_id)
        return TaskRun(**task.model_dump())

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def get_taskrun(self, taskrun_id: str) -> TaskRun | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (taskrun_id,)
        ).fetchone()
        return self._row_to_taskrun(row) if row else None

    def list_taskruns(self) -> list[TaskRun]:
        rows = self._conn.execute("SELECT * FROM task ORDER BY created_at DESC").fetchall()
        return [self._row_to_taskrun(r) for r in rows]

    def get_pending_member_tasks(self, squad_id: str) -> list[Task]:
        """Return non-terminal tasks belonging to squad members (not the leader)."""
        rows = self._conn.execute(
            """SELECT * FROM task
               WHERE squad_id = ?
                 AND status IN ('queued', 'claimed', 'running')
               ORDER BY created_at""",
            (squad_id,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def recover_stale_claims(self, stale_timeout_seconds: float = 60.0) -> int:
        """Move claimed tasks with no heartbeat for stale_timeout back to queued.

        Returns the number of recovered tasks.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - stale_timeout_seconds
        rows = self._conn.execute(
            "SELECT * FROM task WHERE status = 'claimed'"
        ).fetchall()
        recovered = 0
        for row in rows:
            if row["dispatched_at"] is None:
                continue
            dispatched = datetime.fromisoformat(row["dispatched_at"]).timestamp()
            if dispatched < cutoff:
                self._conn.execute(
                    """UPDATE task
                       SET status = 'queued', failure_reason = 'runtime_recovery',
                           runtime_id = NULL, dispatched_at = NULL
                       WHERE id = ?""",
                    (row["id"],),
                )
                recovered += 1
        if recovered:
            self._conn.commit()
        return recovered

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------

    def create_agent(
        self,
        name: str,
        instructions: str,
        backends: list[str],
        skills: list[str],
    ) -> Agent:
        agent = Agent(
            id=_new_id("agent"),
            name=name,
            instructions=instructions,
            backends=backends,
            skills=skills,
        )
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)""",
            (
                agent.id,
                agent.name,
                agent.instructions,
                json.dumps(agent.backends),
                json.dumps(agent.skills),
            ),
        )
        self._conn.commit()
        return agent

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self._conn.execute(
            "SELECT * FROM agent WHERE id = ?", (agent_id,)
        ).fetchone()
        return self._row_to_agent(row) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute("SELECT * FROM agent ORDER BY name").fetchall()
        return [self._row_to_agent(r) for r in rows]

    # ------------------------------------------------------------------
    # Squad
    # ------------------------------------------------------------------

    def create_squad(
        self, name: str, leader_id: str, instructions: str = ""
    ) -> Squad:
        squad = Squad(
            id=_new_id("squad"),
            name=name,
            leader_id=leader_id,
            instructions=instructions,
        )
        self._conn.execute(
            """INSERT INTO squad (id, name, leader_id, instructions)
               VALUES (?, ?, ?, ?)""",
            (squad.id, squad.name, squad.leader_id, squad.instructions),
        )
        self._conn.commit()
        return squad

    def add_squad_member(
        self, squad_id: str, member_id: str, role: str
    ) -> SquadMember:
        sm = SquadMember(
            squad_id=squad_id,
            member_type="agent",
            member_id=member_id,
            role=role,
        )
        self._conn.execute(
            """INSERT INTO squad_member (id, squad_id, member_type, member_id, role)
               VALUES (?, ?, ?, ?, ?)""",
            (_new_id("sm"), sm.squad_id, sm.member_type, sm.member_id, sm.role),
        )
        self._conn.commit()
        return sm

    def get_squad(self, squad_id: str) -> Squad | None:
        row = self._conn.execute(
            "SELECT * FROM squad WHERE id = ?", (squad_id,)
        ).fetchone()
        return self._row_to_squad(row) if row else None

    def get_squad_members(self, squad_id: str) -> list[SquadMember]:
        rows = self._conn.execute(
            "SELECT * FROM squad_member WHERE squad_id = ?", (squad_id,)
        ).fetchall()
        return [self._row_to_squad_member(r) for r in rows]

    def get_squad_leader(self, squad_id: str) -> Agent:
        squad = self.get_squad(squad_id)
        if squad is None:
            raise KeyError(f"squad not found: {squad_id}")
        agent = self.get_agent(squad.leader_id)
        if agent is None:
            raise KeyError(f"leader agent not found: {squad.leader_id}")
        return agent

    # ------------------------------------------------------------------
    # Activity log / trace
    # ------------------------------------------------------------------

    def log_activity(
        self,
        trace_id: str,
        task_id: str | None,
        event: str,
        details: dict | None = None,
    ) -> None:
        """Write an activity log entry for a trace."""
        self._conn.execute(
            """INSERT INTO activity_log (id, trace_id, task_id, event, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _new_id("act"),
                trace_id,
                task_id,
                event,
                json.dumps(details) if details else None,
                _now_iso(),
            ),
        )
        self._conn.commit()

    def get_timeline(self, trace_id: str) -> list[dict]:
        """Return activity log entries for a trace, ordered by time."""
        rows = self._conn.execute(
            "SELECT * FROM activity_log WHERE trace_id = ? ORDER BY created_at",
            (trace_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "trace_id": r["trace_id"],
                "task_id": r["task_id"],
                "event": r["event"],
                "details": json.loads(r["details"]) if r["details"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ]
