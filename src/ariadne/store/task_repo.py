"""Task and TaskRun persistence methods."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ariadne.models import FailureReason, Task, TaskRun, TaskStatus

from .base import _ACTIVE_TASK_STATUS_SQL, _new_id


class TaskRepo:

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
        composed_handoff = self._compose_taskrun_handoff(agent_id, handoff_prompt)
        task = Task(
            id=_new_id(id_prefix),
            issue_id=issue_id,
            agent_id=agent_id,
            squad_id=squad_id,
            status=TaskStatus.QUEUED,
            handoff_prompt=composed_handoff,
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
        self.append_issue_timeline_event(
            task.issue_id,
            "taskrun_queued",
            taskrun_id=task.id,
            payload={"status": "queued", "attempt": task.attempt},
        )
        return task

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        return self.row_to(Task, row) if row else None

    def get_taskrun(self, taskrun_id: str) -> TaskRun | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (taskrun_id,)
        ).fetchone()
        return self.row_to(TaskRun, row) if row else None

    def list_taskruns(self) -> list[TaskRun]:
        rows = self._conn.execute("SELECT * FROM task ORDER BY created_at DESC").fetchall()
        return [self.row_to(TaskRun, r) for r in rows]

    def list_taskruns_for_issue(self, issue_id: str) -> list[TaskRun]:
        rows = self._conn.execute(
            """SELECT * FROM task
               WHERE issue_id = ?
               ORDER BY created_at, id""",
            (issue_id,),
        ).fetchall()
        return [self.row_to(TaskRun, r) for r in rows]

    def get_pending_member_tasks(self, squad_id: str) -> list[Task]:
        """Return non-terminal tasks belonging to squad members (not the leader)."""
        rows = self._conn.execute(
            """SELECT * FROM task
               WHERE squad_id = ?
                 AND status IN ('queued', 'preparing', 'claimed', 'running')
               ORDER BY created_at""",
            (squad_id,),
        ).fetchall()
        return [self.row_to(Task, r) for r in rows]

    # ------------------------------------------------------------------
    # State-machine persistence primitives
    # ------------------------------------------------------------------

    def load_task(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return self.row_to(Task, row)

    def select_claimable_task_rows(self) -> list[sqlite3.Row]:
        """Queued task rows whose issue has no active sibling task."""
        return self._conn.execute(
            f"""SELECT * FROM task
               WHERE status = 'queued'
                 AND NOT EXISTS (
                    SELECT 1 FROM task AS active
                    WHERE active.issue_id = task.issue_id
                      AND active.status IN ({_ACTIVE_TASK_STATUS_SQL})
                 )
               ORDER BY created_at"""
        ).fetchall()

    def select_claimable_task_row_for_agent(
        self, agent_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            f"""SELECT * FROM task
               WHERE status = 'queued' AND agent_id = ?
                 AND NOT EXISTS (
                    SELECT 1 FROM task AS active
                    WHERE active.issue_id = task.issue_id
                      AND active.status IN ({_ACTIVE_TASK_STATUS_SQL})
                 )
               ORDER BY created_at LIMIT 1""",
            (agent_id,),
        ).fetchone()

    def get_agent_backend_preferences(self, agent_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT * FROM agent WHERE id = ?", (agent_id,)
        ).fetchone()
        return json.loads(row["backends"]) if row and row["backends"] else ["dry-run"]

    def get_agent_profile_capacity(self, agent_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT max_concurrent_taskruns FROM agent_profile WHERE id = ?",
            (agent_id,),
        ).fetchone()
        return row["max_concurrent_taskruns"] if row else None

    def count_active_tasks_for_agent(self, agent_id: str) -> int:
        return self._conn.execute(
            f"""SELECT COUNT(*) FROM task
                WHERE agent_id = ?
                  AND status IN ({_ACTIVE_TASK_STATUS_SQL})""",
            (agent_id,),
        ).fetchone()[0]

    def mark_task_claimed(
        self, task_id: str, runtime_id: str, dispatched_at: str
    ) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'claimed', runtime_id = ?, dispatched_at = ?
               WHERE id = ?""",
            (runtime_id, dispatched_at, task_id),
        )

    def mark_task_preparing(
        self, task_id: str, runtime_id: str, dispatched_at: str
    ) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'preparing', runtime_id = ?, dispatched_at = ?
               WHERE id = ?""",
            (runtime_id, dispatched_at, task_id),
        )

    def mark_task_running(self, task_id: str, started_at: str) -> None:
        self._conn.execute(
            "UPDATE task SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, task_id),
        )

    def mark_task_completed(
        self, task_id: str, result: dict, completed_at: str
    ) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'completed', result = ?, completed_at = ?
               WHERE id = ?""",
            (json.dumps(result), completed_at, task_id),
        )

    def mark_task_failed(
        self,
        task_id: str,
        error: str,
        reason: FailureReason,
        completed_at: str,
    ) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'failed', error = ?, failure_reason = ?, completed_at = ?
               WHERE id = ?""",
            (error, reason.value, completed_at, task_id),
        )

    def mark_task_cancelled(self, task_id: str, completed_at: str) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'cancelled', completed_at = ?,
                   failure_reason = 'manual'
               WHERE id = ?""",
            (completed_at, task_id),
        )

    def insert_retry_task(self, task: Task) -> None:
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

    def select_claimed_task_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM task WHERE status = 'claimed'"
        ).fetchall()

    def mark_task_recovered_from_stale_claim(self, task_id: str) -> None:
        self._conn.execute(
            """UPDATE task
               SET status = 'queued', failure_reason = 'runtime_recovery',
                   runtime_id = NULL, dispatched_at = NULL
               WHERE id = ?""",
            (task_id,),
        )
