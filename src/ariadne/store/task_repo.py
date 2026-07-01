"""Task and TaskRun persistence methods."""

from __future__ import annotations

from datetime import datetime, timezone

from ariadne.models import Task, TaskRun, TaskStatus

from .base import _new_id


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
