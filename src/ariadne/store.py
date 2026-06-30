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
    Squad,
    SquadMember,
    Task,
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
        self._lock = __import__("threading").Lock()

        # Migration: add handoff_prompt column if not present
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(task)").fetchall()]
        if "handoff_prompt" not in cols:
            self._conn.execute("ALTER TABLE task ADD COLUMN handoff_prompt TEXT")
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
            created_at=datetime.fromisoformat(row["created_at"]),
        )

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
    ) -> Task:
        task = Task(
            id=_new_id("task"),
            issue_id=issue_id,
            agent_id=agent_id,
            squad_id=squad_id,
            status=TaskStatus.QUEUED,
            handoff_prompt=handoff_prompt,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO task
               (id, issue_id, agent_id, squad_id, status, attempt, max_attempts,
                parent_task_id, failure_reason, handoff_prompt, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                task.created_at.isoformat(),
            ),
        )
        self._conn.commit()
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
            id=_new_id("task"),
            issue_id=old.issue_id,
            agent_id=old.agent_id,
            squad_id=old.squad_id,
            status=TaskStatus.QUEUED,
            attempt=old.attempt + 1,
            max_attempts=old.max_attempts,
            parent_task_id=old.id,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO task
               (id, issue_id, agent_id, squad_id, status, attempt, max_attempts,
                parent_task_id, failure_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                new_task.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return new_task

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

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
