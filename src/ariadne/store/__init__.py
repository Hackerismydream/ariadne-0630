"""SQLite store facade.

The public `Store` API stays import-compatible while implementation moves into
lightweight repository and service layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ariadne.models import (
    Agent,
    FailureReason,
    RuntimeLeaseStatus,
    Squad,
    SquadMember,
    Task,
    TaskRun,
    TaskRunClaim,
    TaskStatus,
)

from .benchmark_repo import BenchmarkRepo
from .issue_repo import IssueRepo
from .runtime_repo import RuntimeRepo
from .skill_repo import SkillRepo
from .base import (
    _ACTIVE_TASK_STATUS_SQL,
    _new_id,
    _now_iso,
    InvalidStateTransition,
    MaxAttemptsExhausted,
    StoreBase,
)


class Store(BenchmarkRepo, IssueRepo, RuntimeRepo, SkillRepo, StoreBase):
    """Backward-compatible facade over the lightweight store layers."""

    def claim_taskrun_for_runtime_machine(
        self,
        runtime_machine_id: str,
        lease_seconds: int = 60,
    ) -> TaskRunClaim | None:
        """Atomically claim the oldest queued TaskRun through a RuntimeLease."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                runtime_machine = self._conn.execute(
                    "SELECT * FROM runtime_machine WHERE id = ?",
                    (runtime_machine_id,),
                ).fetchone()
                if runtime_machine is None:
                    self._conn.execute("COMMIT")
                    return None
                active_for_runtime = self._conn.execute(
                    """SELECT COUNT(*) FROM runtime_lease
                       WHERE runtime_machine_id = ? AND status = 'active'""",
                    (runtime_machine_id,),
                ).fetchone()[0]
                if active_for_runtime >= runtime_machine["max_concurrent_taskruns"]:
                    self._conn.execute("COMMIT")
                    return None
                queued_tasks = self._conn.execute(
                    f"""SELECT * FROM task
                       WHERE status = 'queued'
                         AND NOT EXISTS (
                            SELECT 1 FROM task AS active
                            WHERE active.issue_id = task.issue_id
                              AND active.status IN ({_ACTIVE_TASK_STATUS_SQL})
                         )
                       ORDER BY created_at"""
                ).fetchall()
                if not queued_tasks:
                    self._conn.execute("COMMIT")
                    return None
                capabilities = self._conn.execute(
                    """SELECT * FROM runtime_capability
                       WHERE runtime_machine_id = ? AND status = 'available'
                       ORDER BY provider""",
                    (runtime_machine_id,),
                ).fetchall()
                task = None
                capability = None
                for candidate in queued_tasks:
                    agent = self._conn.execute(
                        "SELECT * FROM agent WHERE id = ?", (candidate["agent_id"],)
                    ).fetchone()
                    desired = (
                        json.loads(agent["backends"])
                        if agent and agent["backends"]
                        else ["dry-run"]
                    )
                    for provider in desired or ["dry-run"]:
                        capability = next(
                            (
                                cap
                                for cap in capabilities
                                if cap["provider"] == provider
                            ),
                            None,
                        )
                        if capability is not None:
                            task = candidate
                            break
                    if capability is None and capabilities:
                        capability = capabilities[0]
                        task = candidate
                    if task is not None and not self._agent_capacity_available(
                        task["agent_id"]
                    ):
                        task = None
                        capability = None
                        continue
                    if task is not None:
                        break
                if task is None or capability is None:
                    self._conn.execute("COMMIT")
                    return None
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                lease_id = _new_id("lease")
                self._conn.execute(
                    """INSERT INTO runtime_lease
                       (id, taskrun_id, runtime_machine_id, runtime_capability_id,
                        status, lease_token, acquired_at, last_heartbeat_at,
                        expires_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        lease_id,
                        task["id"],
                        runtime_machine_id,
                        capability["id"],
                        RuntimeLeaseStatus.ACTIVE.value,
                        _new_id("lease-token"),
                        now,
                        now,
                        (now_dt + timedelta(seconds=lease_seconds)).isoformat(),
                        "{}",
                    ),
                )
                self._conn.execute(
                    """UPDATE task
                       SET status = 'preparing', runtime_id = ?, dispatched_at = ?
                       WHERE id = ?""",
                    (runtime_machine_id, now, task["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self.append_issue_timeline_event(
            task["issue_id"],
            "lease_acquired",
            actor_type="runtime",
            actor_id=runtime_machine_id,
            taskrun_id=task["id"],
            runtime_lease_id=lease_id,
        )
        self.append_issue_timeline_event(
            task["issue_id"],
            "taskrun_preparing",
            actor_type="runtime",
            actor_id=runtime_machine_id,
            taskrun_id=task["id"],
            runtime_lease_id=lease_id,
            payload={"status": "preparing"},
        )
        taskrun = self.get_taskrun(task["id"])
        lease = self.get_runtime_lease(lease_id)
        if taskrun is None or lease is None:
            raise KeyError("claim was committed but could not be reloaded")
        return TaskRunClaim(taskrun=taskrun, lease=lease)

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

    def claim_task(self, agent_id: str, runtime_id: str) -> Task | None:
        """Atomically claim the oldest queued task for the given agent.

        Uses BEGIN IMMEDIATE to serialize concurrent claims — the write
        lock prevents two daemons from claiming the same task.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
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
            return self.row_to(Task, 
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
        task = self._load_task(task_id)
        self.append_issue_timeline_event(
            task.issue_id,
            "taskrun_started",
            actor_type="runtime",
            actor_id=task.runtime_id,
            taskrun_id=task.id,
            payload={"status": "running"},
        )
        return task

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
        task = self._load_task(task_id)
        self.append_issue_timeline_event(
            task.issue_id,
            "taskrun_completed",
            actor_type="agent",
            actor_id=task.agent_id,
            taskrun_id=task.id,
            payload={"result": result},
        )
        return task

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
        task = self._load_task(task_id)
        self.append_issue_timeline_event(
            task.issue_id,
            "taskrun_failed",
            actor_type="agent",
            actor_id=task.agent_id,
            taskrun_id=task.id,
            payload={"error": error, "failure_reason": reason.value},
        )
        return task

    def fail_taskrun(
        self, taskrun_id: str, error: str, reason: FailureReason
    ) -> TaskRun:
        task = self.fail_task(taskrun_id, error, reason)
        return TaskRun(**task.model_dump())

    def cancel_task(self, task_id: str) -> Task:
        task = self._load_task(task_id)
        # Cancel is only legal from running (per design doc).
        # But we also allow cancelling queued/claimed for user-initiated cancel.
        if task.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PREPARING,
            TaskStatus.CLAIMED,
            TaskStatus.RUNNING,
        ):
            raise InvalidStateTransition(task.status.value, "cancel_task")
        now = _now_iso()
        self._conn.execute(
            """UPDATE task
               SET status = 'cancelled', completed_at = ?,
                   failure_reason = 'manual'
               WHERE id = ?""",
            (now, task_id),
        )
        lease = self.get_active_runtime_lease_for_taskrun(task_id)
        if lease is not None:
            self._conn.execute(
                """UPDATE runtime_lease
                   SET status = 'revoked', released_at = ?, revoke_reason = ?
                   WHERE id = ?""",
                (now, "taskrun_cancelled", lease.id),
            )
        self._conn.commit()
        if lease is not None:
            self.append_issue_timeline_event(
                task.issue_id,
                "lease_revoked",
                actor_type="system",
                taskrun_id=task.id,
                runtime_lease_id=lease.id,
                payload={"reason": "taskrun_cancelled"},
            )
        self.append_issue_timeline_event(
            task.issue_id,
            "taskrun_cancelled",
            actor_type="system",
            taskrun_id=task.id,
            payload={"status": "cancelled"},
        )
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
        self.append_issue_timeline_event(
            old.issue_id,
            "retry_scheduled",
            actor_type="system",
            taskrun_id=old.id,
            payload={"retry_taskrun_id": new_task.id, "attempt": new_task.attempt},
        )
        self.append_issue_timeline_event(
            new_task.issue_id,
            "taskrun_queued",
            actor_type="system",
            taskrun_id=new_task.id,
            payload={"status": "queued", "attempt": new_task.attempt},
        )
        return new_task

    def retry_taskrun(self, taskrun_id: str) -> TaskRun:
        task = self.retry_task(taskrun_id)
        return TaskRun(**task.model_dump())

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
        return self.row_to(Squad, row) if row else None

    def get_squad_members(self, squad_id: str) -> list[SquadMember]:
        rows = self._conn.execute(
            "SELECT * FROM squad_member WHERE squad_id = ?", (squad_id,)
        ).fetchall()
        return [self.row_to(SquadMember, r) for r in rows]

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
