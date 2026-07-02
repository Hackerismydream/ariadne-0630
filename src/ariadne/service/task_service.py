"""Task state-machine and claim orchestration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ariadne.models import (
    FailureReason,
    Task,
    TaskRun,
    TaskRunClaim,
    TaskStatus,
)
from ariadne.store.base import (
    _LEGAL_TRANSITIONS,
    _new_id,
    _now_iso,
    InvalidStateTransition,
    MaxAttemptsExhausted,
)


class TaskService:
    """Business rules for task claiming and state transitions."""

    def __init__(self, store: Any):
        self.store = store

    def claim_taskrun_for_runtime_machine(
        self,
        runtime_machine_id: str,
        lease_seconds: int = 60,
    ) -> TaskRunClaim | None:
        """Atomically claim the oldest queued TaskRun through a RuntimeLease."""
        task: sqlite3.Row | None = None
        lease_id: str | None = None

        with self.store.transaction():
            runtime_machine = self.store.get_runtime_machine(runtime_machine_id)
            if runtime_machine is None:
                return None
            active_for_runtime = self.store.count_active_runtime_leases(
                runtime_machine_id
            )
            if active_for_runtime >= runtime_machine.max_concurrent_taskruns:
                return None

            capabilities = self.store.select_available_runtime_capability_rows(
                runtime_machine_id
            )
            for candidate in self.store.select_claimable_task_rows():
                capability = self._match_capability(candidate, capabilities)
                if capability is None:
                    continue
                if not self._agent_capacity_available(candidate["agent_id"]):
                    continue

                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                lease_id = _new_id("lease")
                self.store.insert_runtime_lease(
                    lease_id=lease_id,
                    taskrun_id=candidate["id"],
                    runtime_machine_id=runtime_machine_id,
                    runtime_capability_id=capability["id"],
                    lease_token=_new_id("lease-token"),
                    acquired_at=now,
                    expires_at=(now_dt + timedelta(seconds=lease_seconds)).isoformat(),
                )
                self.store.mark_task_preparing(candidate["id"], runtime_machine_id, now)
                task = candidate
                break

            if task is None or lease_id is None:
                return None

        self.store.append_issue_timeline_event(
            task["issue_id"],
            "lease_acquired",
            actor_type="runtime",
            actor_id=runtime_machine_id,
            taskrun_id=task["id"],
            runtime_lease_id=lease_id,
        )
        self.store.append_issue_timeline_event(
            task["issue_id"],
            "taskrun_preparing",
            actor_type="runtime",
            actor_id=runtime_machine_id,
            taskrun_id=task["id"],
            runtime_lease_id=lease_id,
            payload={"status": "preparing"},
        )
        taskrun = self.store.get_taskrun(task["id"])
        lease = self.store.get_runtime_lease(lease_id)
        if taskrun is None or lease is None:
            raise KeyError("claim was committed but could not be reloaded")
        return TaskRunClaim(taskrun=taskrun, lease=lease)

    def claim_task(self, agent_id: str, runtime_id: str) -> Task | None:
        """Atomically claim the oldest queued task for the given agent."""
        task_id: str | None = None
        with self.store.transaction():
            row = self.store.select_claimable_task_row_for_agent(agent_id)
            if row is None:
                return None
            now = _now_iso()
            self.store.mark_task_claimed(row["id"], runtime_id, now)
            task_id = row["id"]
        return self.store.get_task(task_id) if task_id else None

    def claim_taskrun(self, agent_profile_id: str, runtime_id: str) -> TaskRun | None:
        task = self.claim_task(agent_profile_id, runtime_id)
        return TaskRun(**task.model_dump()) if task else None

    def start_task(self, task_id: str) -> Task:
        task = self.store.load_task(task_id)
        self._check_transition(task.status, TaskStatus.RUNNING, "start_task")
        now = _now_iso()
        with self.store.transaction():
            self.store.mark_task_running(task_id, now)
        task = self.store.load_task(task_id)
        self.store.append_issue_timeline_event(
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
        task = self.store.load_task(task_id)
        self._check_transition(task.status, TaskStatus.COMPLETED, "complete_task")
        now = _now_iso()
        with self.store.transaction():
            self.store.mark_task_completed(task_id, result, now)
        task = self.store.load_task(task_id)
        self.store.append_issue_timeline_event(
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
        task = self.store.load_task(task_id)
        self._check_transition(task.status, TaskStatus.FAILED, "fail_task")
        now = _now_iso()
        with self.store.transaction():
            self.store.mark_task_failed(task_id, error, reason, now)
        task = self.store.load_task(task_id)
        self.store.append_issue_timeline_event(
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
        task = self.store.load_task(task_id)
        if task.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PREPARING,
            TaskStatus.CLAIMED,
            TaskStatus.RUNNING,
        ):
            raise InvalidStateTransition(task.status.value, "cancel_task")
        now = _now_iso()
        lease = None
        with self.store.transaction():
            self.store.mark_task_cancelled(task_id, now)
            lease = self.store.get_active_runtime_lease_for_taskrun(task_id)
            if lease is not None:
                self.store.mark_runtime_lease_revoked(
                    lease.id, now, "taskrun_cancelled"
                )

        if lease is not None:
            self.store.append_issue_timeline_event(
                task.issue_id,
                "lease_revoked",
                actor_type="system",
                taskrun_id=task.id,
                runtime_lease_id=lease.id,
                payload={"reason": "taskrun_cancelled"},
            )
        self.store.append_issue_timeline_event(
            task.issue_id,
            "taskrun_cancelled",
            actor_type="system",
            taskrun_id=task.id,
            payload={"status": "cancelled"},
        )
        return self.store.load_task(task_id)

    def cancel_taskrun(self, taskrun_id: str) -> TaskRun:
        task = self.cancel_task(taskrun_id)
        return TaskRun(**task.model_dump())

    def retry_task(self, task_id: str) -> Task:
        old = self.store.load_task(task_id)
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
            timeout_seconds=old.timeout_seconds,
            target_repo_path=old.target_repo_path,
            parent_task_id=old.id,
            handoff_prompt=old.handoff_prompt,
            trace_id=old.trace_id,
            created_at=datetime.now(timezone.utc),
        )
        with self.store.transaction():
            self.store.insert_retry_task(new_task)
        self.store.log_activity(
            new_task.trace_id,
            new_task.id,
            "retried",
            {"attempt": new_task.attempt, "parent": old.id},
        )
        self.store.append_issue_timeline_event(
            old.issue_id,
            "retry_scheduled",
            actor_type="system",
            taskrun_id=old.id,
            payload={"retry_taskrun_id": new_task.id, "attempt": new_task.attempt},
        )
        self.store.append_issue_timeline_event(
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

    def recover_stale_claims(self, stale_timeout_seconds: float = 60.0) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - stale_timeout_seconds
        recovered = 0
        with self.store.transaction():
            rows = self.store.select_claimed_task_rows()
            for row in rows:
                if row["dispatched_at"] is None:
                    continue
                dispatched = datetime.fromisoformat(row["dispatched_at"]).timestamp()
                if dispatched < cutoff:
                    self.store.mark_task_recovered_from_stale_claim(row["id"])
                    recovered += 1
        return recovered

    def _match_capability(
        self,
        task: sqlite3.Row,
        capabilities: list[sqlite3.Row],
    ) -> sqlite3.Row | None:
        desired = self.store.get_agent_backend_preferences(task["agent_id"])
        for provider in desired or ["dry-run"]:
            capability = next(
                (cap for cap in capabilities if cap["provider"] == provider),
                None,
            )
            if capability is not None:
                return capability
        return None

    def _agent_capacity_available(self, agent_id: str) -> bool:
        capacity = self.store.get_agent_profile_capacity(agent_id)
        if capacity is None:
            return True
        active_for_profile = self.store.count_active_tasks_for_agent(agent_id)
        return active_for_profile < capacity

    def _check_transition(
        self, current: TaskStatus, target: TaskStatus, action: str
    ) -> None:
        if (current, target) not in _LEGAL_TRANSITIONS:
            raise InvalidStateTransition(current.value, action)
