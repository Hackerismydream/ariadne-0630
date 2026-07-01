"""Runtime lease lifecycle orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ariadne.models import (
    FailureReason,
    RuntimeLease,
    RuntimeLeaseStatus,
    Task,
    TaskStatus,
)
from ariadne.store.base import _now_iso


class LeaseService:
    """Business rules for runtime lease heartbeat, release, revoke, and expiry."""

    def __init__(self, store: Any):
        self.store = store

    def heartbeat_runtime_lease(
        self, lease_id: str, lease_seconds: int = 60
    ) -> RuntimeLease:
        now_dt = datetime.now(timezone.utc)
        with self.store.transaction():
            self.store.touch_runtime_lease(
                lease_id,
                heartbeat_at=now_dt.isoformat(),
                expires_at=(now_dt + timedelta(seconds=lease_seconds)).isoformat(),
            )
        lease = self.store.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        return lease

    def release_runtime_lease(self, lease_id: str) -> RuntimeLease:
        now = _now_iso()
        with self.store.transaction():
            self.store.mark_runtime_lease_released(lease_id, now)
        lease = self.store.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        task = self.store.get_task(lease.taskrun_id)
        if task and lease.status == RuntimeLeaseStatus.RELEASED:
            self.store.append_issue_timeline_event(
                task.issue_id,
                "lease_released",
                actor_type="runtime",
                actor_id=lease.runtime_machine_id,
                taskrun_id=task.id,
                runtime_lease_id=lease.id,
            )
        return lease

    def revoke_runtime_lease(
        self, lease_id: str, reason: str = "revoked"
    ) -> RuntimeLease:
        now = _now_iso()
        with self.store.transaction():
            self.store.mark_runtime_lease_revoked(
                lease_id,
                released_at=now,
                reason=reason,
                active_only=True,
            )
        lease = self.store.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        task = self.store.get_task(lease.taskrun_id)
        if task and lease.status == RuntimeLeaseStatus.REVOKED:
            self.store.append_issue_timeline_event(
                task.issue_id,
                "lease_revoked",
                actor_type="system",
                taskrun_id=task.id,
                runtime_lease_id=lease.id,
                payload={"reason": reason},
            )
        return lease

    def expire_runtime_leases(self) -> list[RuntimeLease]:
        now_dt = datetime.now(timezone.utc)
        expired: list[RuntimeLease] = []
        failure_events: list[tuple[Task, str]] = []
        with self.store.transaction():
            rows = self.store.select_expired_runtime_lease_rows(now_dt.isoformat())
            for row in rows:
                self.store.mark_runtime_lease_expired(row["id"])
                task = self.store.get_task(row["taskrun_id"])
                if task and task.status in (
                    TaskStatus.PREPARING,
                    TaskStatus.RUNNING,
                    TaskStatus.CLAIMED,
                ):
                    self.store.mark_task_failed(
                        task.id,
                        "runtime lease expired",
                        FailureReason.RUNTIME_OFFLINE,
                        now_dt.isoformat(),
                    )
                    failure_events.append((task, row["id"]))
                expired.append(
                    RuntimeLease(
                        **{
                            **self.store.row_to(RuntimeLease, row).model_dump(),
                            "status": RuntimeLeaseStatus.EXPIRED,
                        }
                    )
                )

        for task, lease_id in failure_events:
            self.store.append_issue_timeline_event(
                task.issue_id,
                "lease_expired",
                actor_type="system",
                taskrun_id=task.id,
                runtime_lease_id=lease_id,
            )
            self.store.append_issue_timeline_event(
                task.issue_id,
                "taskrun_failed",
                actor_type="system",
                taskrun_id=task.id,
                runtime_lease_id=lease_id,
                payload={
                    "error": "runtime lease expired",
                    "failure_reason": FailureReason.RUNTIME_OFFLINE.value,
                },
            )
        return expired
