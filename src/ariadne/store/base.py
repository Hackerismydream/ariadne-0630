"""Shared SQLite store infrastructure."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TypeVar

from pydantic import BaseModel

from ariadne.models import (
    Agent,
    AgentProfile,
    BenchmarkRun,
    IssueTimelineEvent,
    LeaderDecision,
    RuntimeCapability,
    RuntimeLease,
    RuntimeMachine,
    Skill,
    Task,
    TaskRun,
    TaskStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_MAX_CONCURRENT_TASKRUNS = 4
DEFAULT_AGENT_PROFILE_MAX_CONCURRENT_TASKRUNS = 1
_ACTIVE_TASK_STATUS_SQL = "'claimed', 'preparing', 'running'"

T = TypeVar("T", bound=BaseModel)


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


_LEGAL_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (TaskStatus.QUEUED, TaskStatus.CLAIMED),
    (TaskStatus.QUEUED, TaskStatus.PREPARING),
    (TaskStatus.PREPARING, TaskStatus.RUNNING),
    (TaskStatus.PREPARING, TaskStatus.FAILED),
    (TaskStatus.PREPARING, TaskStatus.CANCELLED),
    (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    (TaskStatus.CLAIMED, TaskStatus.QUEUED),
    (TaskStatus.RUNNING, TaskStatus.COMPLETED),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_machine (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('online', 'offline', 'draining', 'disabled')),
    version TEXT NOT NULL DEFAULT '',
    device_info TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at TEXT,
    max_concurrent_taskruns INTEGER NOT NULL DEFAULT 4,
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

CREATE TABLE IF NOT EXISTS runtime_lease (
    id TEXT PRIMARY KEY,
    taskrun_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    runtime_machine_id TEXT NOT NULL REFERENCES runtime_machine(id),
    runtime_capability_id TEXT NOT NULL REFERENCES runtime_capability(id),
    status TEXT NOT NULL
        CHECK (status IN ('active', 'released', 'expired', 'revoked')),
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    released_at TEXT,
    expires_at TEXT NOT NULL,
    revoke_reason TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_lease_one_active
    ON runtime_lease(taskrun_id) WHERE status = 'active';

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

CREATE TABLE IF NOT EXISTS issue_timeline_event (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    taskrun_id TEXT REFERENCES task(id),
    runtime_lease_id TEXT REFERENCES runtime_lease(id),
    leader_decision_id TEXT,
    comment_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issue_timeline_issue_time
    ON issue_timeline_event(issue_id, created_at);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    squad_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'preparing', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
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
    result TEXT,
    error TEXT,
    runtime_id TEXT,
    handoff_prompt TEXT,
    trace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_claim
    ON task(status, created_at) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS leader_decision (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    squad_id TEXT NOT NULL REFERENCES squad(id) ON DELETE CASCADE,
    leader_task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('action', 'no_action', 'failed', 'done')),
    reason TEXT NOT NULL DEFAULT '',
    delegation_payload_json TEXT NOT NULL DEFAULT '{}',
    created_taskrun_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leader_decision_issue_time
    ON leader_decision(issue_id, created_at);

CREATE TABLE IF NOT EXISTS benchmark_run (
    id TEXT PRIMARY KEY,
    suite_name TEXT NOT NULL,
    case_name TEXT NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    artifact_dir TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_benchmark_run_suite_case
    ON benchmark_run(suite_name, case_name, started_at);

CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    task_id TEXT,
    event TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_log(trace_id);

CREATE TABLE IF NOT EXISTS agent_profile (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    preferred_capabilities_json TEXT NOT NULL DEFAULT '[]',
    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
    max_concurrent_taskruns INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    when_to_use TEXT NOT NULL DEFAULT '',
    prompt_snippet TEXT NOT NULL DEFAULT '',
    tools_allowed_json TEXT NOT NULL DEFAULT '[]',
    test_command TEXT,
    source_path TEXT,
    version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profile_skill (
    agent_profile_id TEXT NOT NULL REFERENCES agent_profile(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_profile_id, skill_id)
);

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

_JSON_ALIASES: dict[type[BaseModel], dict[str, str]] = {
    RuntimeCapability: {"models": "models_json", "default_args": "default_args_json"},
    IssueTimelineEvent: {"payload": "payload_json"},
    LeaderDecision: {
        "delegation_payload": "delegation_payload_json",
        "created_taskrun_ids": "created_taskrun_ids_json",
    },
    BenchmarkRun: {
        "runtime_policy": "runtime_policy_json",
        "summary": "summary_json",
        "metrics": "metrics_json",
    },
    AgentProfile: {
        "preferred_capabilities": "preferred_capabilities_json",
        "runtime_policy": "runtime_policy_json",
    },
    Skill: {"tools_allowed": "tools_allowed_json"},
}

_JSON_FIELDS: dict[type[BaseModel], set[str]] = {
    RuntimeMachine: {"device_info", "repo_allowlist", "metadata"},
    RuntimeCapability: {"models", "default_args", "metadata"},
    RuntimeLease: {"metadata"},
    IssueTimelineEvent: {"payload"},
    Task: {"result"},
    TaskRun: {"result"},
    LeaderDecision: {"delegation_payload", "created_taskrun_ids"},
    BenchmarkRun: {"runtime_policy", "summary", "metrics"},
    AgentProfile: {"preferred_capabilities", "runtime_policy"},
    Skill: {"tools_allowed"},
    Agent: {"backends", "skills"},
}


class StoreBase:
    """Connection, schema, transaction, and row conversion shared by repos."""

    def __init__(self, db_path: str = "ariadne.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

        self._migrate_task_table_if_needed()
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(task)").fetchall()]
        if "handoff_prompt" not in cols:
            self._conn.execute("ALTER TABLE task ADD COLUMN handoff_prompt TEXT")
            self._conn.commit()
        if "trace_id" not in cols:
            self._conn.execute("ALTER TABLE task ADD COLUMN trace_id TEXT")
            self._conn.commit()
        self._ensure_task_one_active_per_issue_index()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run multiple repo operations under one BEGIN IMMEDIATE transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def row_to(self, model_cls: type[T], row: sqlite3.Row) -> T:
        raw = dict(row)
        aliases = _JSON_ALIASES.get(model_cls, {})
        json_fields = _JSON_FIELDS.get(model_cls, set())
        data = {}
        for field_name in model_cls.model_fields:
            column_name = aliases.get(field_name, field_name)
            if column_name not in raw:
                continue
            value = raw[column_name]
            if value is not None and field_name in json_fields:
                value = json.loads(value)
            data[field_name] = value
        return model_cls(**data)

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return self.row_to(Task, row)

    def _row_to_taskrun(self, row: sqlite3.Row) -> TaskRun:
        return self.row_to(TaskRun, row)

    def _migrate_task_table_if_needed(self) -> None:
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task'"
        ).fetchone()
        if row is None:
            return
        table_sql = row["sql"] or ""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(task)").fetchall()]
        needs_rebuild = (
            "preparing" not in table_sql
            or "policy_blocked" not in table_sql
            or "trace_id" not in cols
            or "test_failure" not in table_sql
        )
        if not needs_rebuild:
            return

        select_exprs = []
        for column in (
            "id", "issue_id", "agent_id", "squad_id", "status", "attempt",
            "max_attempts", "parent_task_id", "failure_reason", "dispatched_at",
            "started_at", "completed_at", "result", "error", "runtime_id",
            "handoff_prompt", "trace_id", "created_at",
        ):
            select_exprs.append(column if column in cols else f"NULL AS {column}")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("DROP INDEX IF EXISTS idx_task_claim")
        self._conn.execute("DROP INDEX IF EXISTS idx_task_one_active_per_issue")
        self._conn.execute(
            """CREATE TABLE task_new (
                id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                squad_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'preparing', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
                attempt INTEGER NOT NULL DEFAULT 1,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
                failure_reason TEXT
                    CHECK (failure_reason IS NULL OR failure_reason IN
                           ('agent_error', 'timeout', 'runtime_offline',
                            'runtime_recovery', 'manual', 'policy_blocked',
                            'provider_error', 'test_failure', 'routing_failure',
                            'llm_parse_failure')),
                dispatched_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT,
                runtime_id TEXT,
                handoff_prompt TEXT,
                trace_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        self._conn.execute(
            f"""INSERT INTO task_new
               (id, issue_id, agent_id, squad_id, status, attempt, max_attempts,
                parent_task_id, failure_reason, dispatched_at, started_at,
                completed_at, result, error, runtime_id, handoff_prompt,
                trace_id, created_at)
               SELECT {", ".join(select_exprs)} FROM task"""
        )
        self._conn.execute("DROP TABLE task")
        self._conn.execute("ALTER TABLE task_new RENAME TO task")
        self._conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_task_claim
               ON task(status, created_at) WHERE status = 'queued'"""
        )
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    def _ensure_task_one_active_per_issue_index(self) -> None:
        duplicates = self._conn.execute(
            f"""SELECT issue_id, COUNT(*) AS active_count
                FROM task
                WHERE status IN ({_ACTIVE_TASK_STATUS_SQL})
                GROUP BY issue_id
                HAVING active_count > 1"""
        ).fetchall()
        for duplicate in duplicates:
            rows = self._conn.execute(
                f"""SELECT id FROM task
                    WHERE issue_id = ?
                      AND status IN ({_ACTIVE_TASK_STATUS_SQL})
                    ORDER BY COALESCE(started_at, dispatched_at, created_at), id""",
                (duplicate["issue_id"],),
            ).fetchall()
            keep_id = rows[0]["id"]
            failed_ids = [row["id"] for row in rows[1:]]
            logger.warning(
                "resolving duplicate active tasks for one issue: "
                "issue_id=%s keeping=%s failing=%s",
                duplicate["issue_id"], keep_id, failed_ids,
            )
            now = _now_iso()
            placeholders = ", ".join("?" for _ in failed_ids)
            self._conn.execute(
                f"""UPDATE task
                    SET status = 'failed',
                        failure_reason = 'runtime_recovery',
                        error = ?,
                        completed_at = ?
                    WHERE id IN ({placeholders})""",
                (
                    "migration deactivated duplicate active task for "
                    "per-issue serialization",
                    now,
                    *failed_ids,
                ),
            )
            self._conn.execute(
                f"""UPDATE runtime_lease
                    SET status = 'revoked',
                        released_at = ?,
                        revoke_reason = 'per_issue_serialization_migration'
                    WHERE status = 'active'
                      AND taskrun_id IN ({placeholders})""",
                (now, *failed_ids),
            )
        if duplicates:
            self._conn.commit()

        self._conn.execute(
            f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_task_one_active_per_issue
                ON task(issue_id)
                WHERE status IN ({_ACTIVE_TASK_STATUS_SQL})"""
        )
        self._conn.commit()

    def _load_task(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return self.row_to(Task, row)

    def _check_transition(
        self, current: TaskStatus, target: TaskStatus, action: str
    ) -> None:
        if (current, target) not in _LEGAL_TRANSITIONS:
            raise InvalidStateTransition(current.value, action)

    def _agent_capacity_available(self, agent_id: str) -> bool:
        profile = self._conn.execute(
            "SELECT max_concurrent_taskruns FROM agent_profile WHERE id = ?",
            (agent_id,),
        ).fetchone()
        if profile is None:
            return True
        active_for_profile = self._conn.execute(
            f"""SELECT COUNT(*) FROM task
                WHERE agent_id = ?
                  AND status IN ({_ACTIVE_TASK_STATUS_SQL})""",
            (agent_id,),
        ).fetchone()[0]
        return active_for_profile < profile["max_concurrent_taskruns"]
