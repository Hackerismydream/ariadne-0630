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

from .schema import _SCHEMA

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
    (TaskStatus.QUEUED, TaskStatus.CANCELLED),
    (TaskStatus.PREPARING, TaskStatus.RUNNING),
    (TaskStatus.PREPARING, TaskStatus.FAILED),
    (TaskStatus.PREPARING, TaskStatus.CANCELLED),
    (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    (TaskStatus.CLAIMED, TaskStatus.QUEUED),
    (TaskStatus.CLAIMED, TaskStatus.CANCELLED),
    (TaskStatus.RUNNING, TaskStatus.COMPLETED),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

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
        self._conn.execute("PRAGMA busy_timeout=5000")
        if db_path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                logger.debug("Skipped WAL setup because database is locked")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

        self._migrate_issue_table_if_needed()
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

    def _migrate_issue_table_if_needed(self) -> None:
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'issue'"
        ).fetchone()
        if row is None:
            return
        table_sql = row["sql"] or ""
        if "'failed'" in table_sql:
            return

        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute(
            """CREATE TABLE issue_new (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'backlog'
                    CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'failed', 'cancelled')),
                assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
                assignee_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """INSERT INTO issue_new
               (id, title, description, status, assignee_type, assignee_id, created_at)
               SELECT id, title, description, status, assignee_type, assignee_id, created_at
               FROM issue"""
        )
        self._conn.execute("DROP TABLE issue")
        self._conn.execute("ALTER TABLE issue_new RENAME TO issue")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

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
            or "timeout_seconds" not in cols
            or "target_repo_path" not in cols
            or "test_failure" not in table_sql
        )
        if not needs_rebuild:
            return

        select_exprs = []
        for column in (
            "id", "issue_id", "agent_id", "squad_id", "status", "attempt",
            "max_attempts", "timeout_seconds", "target_repo_path", "parent_task_id",
            "failure_reason", "dispatched_at", "started_at", "completed_at",
            "result", "error", "runtime_id", "handoff_prompt", "trace_id",
            "created_at",
        ):
            default = "600" if column == "timeout_seconds" else "NULL"
            select_exprs.append(column if column in cols else f"{default} AS {column}")
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
                timeout_seconds INTEGER NOT NULL DEFAULT 600,
                target_repo_path TEXT,
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
                timeout_seconds, target_repo_path, parent_task_id, failure_reason,
                dispatched_at, started_at, completed_at, result, error, runtime_id,
                handoff_prompt,
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
