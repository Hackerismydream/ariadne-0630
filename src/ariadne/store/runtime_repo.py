"""RuntimeMachine, RuntimeCapability, and RuntimeLease persistence methods."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ariadne.models import (
    FailureReason,
    RuntimeCapability,
    RuntimeCapabilityStatus,
    RuntimeLease,
    RuntimeLeaseStatus,
    RuntimeMachine,
    RuntimeMachineStatus,
    TaskStatus,
)

from .base import DEFAULT_RUNTIME_MAX_CONCURRENT_TASKRUNS, _new_id, _now_iso


class RuntimeRepo:

    # ------------------------------------------------------------------
    # RuntimeMachine / RuntimeCapability
    # ------------------------------------------------------------------

    def register_runtime_machine(
        self,
        runtime_machine_id: str,
        name: str,
        version: str = "",
        workspace_root: str = "",
        max_concurrent_taskruns: int = DEFAULT_RUNTIME_MAX_CONCURRENT_TASKRUNS,
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
        return self.row_to(RuntimeMachine, row) if row else None

    def list_runtime_machines(self) -> list[RuntimeMachine]:
        rows = self._conn.execute(
            "SELECT * FROM runtime_machine ORDER BY name"
        ).fetchall()
        return [self.row_to(RuntimeMachine, r) for r in rows]

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
        return self.row_to(RuntimeCapability, row)

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
        return [self.row_to(RuntimeCapability, r) for r in rows]

    def get_runtime_capability(self, capability_id: str) -> RuntimeCapability | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_capability WHERE id = ?", (capability_id,)
        ).fetchone()
        return self.row_to(RuntimeCapability, row) if row else None

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
        return self.row_to(RuntimeCapability, row)

    def get_runtime_lease(self, lease_id: str) -> RuntimeLease | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_lease WHERE id = ?", (lease_id,)
        ).fetchone()
        return self.row_to(RuntimeLease, row) if row else None

    def get_active_runtime_lease_for_taskrun(
        self, taskrun_id: str
    ) -> RuntimeLease | None:
        row = self._conn.execute(
            """SELECT * FROM runtime_lease
               WHERE taskrun_id = ? AND status = 'active'
               ORDER BY acquired_at DESC LIMIT 1""",
            (taskrun_id,),
        ).fetchone()
        return self.row_to(RuntimeLease, row) if row else None

    def list_runtime_leases(self, taskrun_id: str | None = None) -> list[RuntimeLease]:
        if taskrun_id is None:
            rows = self._conn.execute(
                "SELECT * FROM runtime_lease ORDER BY acquired_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM runtime_lease
                   WHERE taskrun_id = ?
                   ORDER BY acquired_at""",
                (taskrun_id,),
            ).fetchall()
        return [self.row_to(RuntimeLease, r) for r in rows]

    def heartbeat_runtime_lease(
        self, lease_id: str, lease_seconds: int = 60
    ) -> RuntimeLease:
        now_dt = datetime.now(timezone.utc)
        self._conn.execute(
            """UPDATE runtime_lease
               SET last_heartbeat_at = ?, expires_at = ?
               WHERE id = ? AND status = 'active'""",
            (
                now_dt.isoformat(),
                (now_dt + timedelta(seconds=lease_seconds)).isoformat(),
                lease_id,
            ),
        )
        self._conn.commit()
        lease = self.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        return lease

    def release_runtime_lease(self, lease_id: str) -> RuntimeLease:
        now = _now_iso()
        self._conn.execute(
            """UPDATE runtime_lease
               SET status = 'released', released_at = ?
               WHERE id = ? AND status = 'active'""",
            (now, lease_id),
        )
        self._conn.commit()
        lease = self.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        task = self.get_task(lease.taskrun_id)
        if task and lease.status == RuntimeLeaseStatus.RELEASED:
            self.append_issue_timeline_event(
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
        self._conn.execute(
            """UPDATE runtime_lease
               SET status = 'revoked', released_at = ?, revoke_reason = ?
               WHERE id = ? AND status = 'active'""",
            (now, reason, lease_id),
        )
        self._conn.commit()
        lease = self.get_runtime_lease(lease_id)
        if lease is None:
            raise KeyError(f"runtime lease not found: {lease_id}")
        task = self.get_task(lease.taskrun_id)
        if task and lease.status == RuntimeLeaseStatus.REVOKED:
            self.append_issue_timeline_event(
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
        rows = self._conn.execute(
            """SELECT * FROM runtime_lease
               WHERE status = 'active' AND expires_at < ?
               ORDER BY expires_at""",
            (now_dt.isoformat(),),
        ).fetchall()
        expired: list[RuntimeLease] = []
        for row in rows:
            self._conn.execute(
                "UPDATE runtime_lease SET status = 'expired' WHERE id = ?",
                (row["id"],),
            )
            task = self.get_task(row["taskrun_id"])
            if task and task.status in (
                TaskStatus.PREPARING,
                TaskStatus.RUNNING,
                TaskStatus.CLAIMED,
            ):
                self._conn.execute(
                    """UPDATE task
                       SET status = 'failed', failure_reason = ?,
                           error = ?, completed_at = ?
                       WHERE id = ?""",
                    (
                        FailureReason.RUNTIME_OFFLINE.value,
                        "runtime lease expired",
                        now_dt.isoformat(),
                        task.id,
                    ),
                )
                self.append_issue_timeline_event(
                    task.issue_id,
                    "lease_expired",
                    actor_type="system",
                    taskrun_id=task.id,
                    runtime_lease_id=row["id"],
                )
                self.append_issue_timeline_event(
                    task.issue_id,
                    "taskrun_failed",
                    actor_type="system",
                    taskrun_id=task.id,
                    runtime_lease_id=row["id"],
                    payload={
                        "error": "runtime lease expired",
                        "failure_reason": FailureReason.RUNTIME_OFFLINE.value,
                    },
                )
            expired.append(
                RuntimeLease(
                    **{
                        **self.row_to(RuntimeLease, row).model_dump(),
                        "status": RuntimeLeaseStatus.EXPIRED,
                    }
                )
            )
        if rows:
            self._conn.commit()
        return expired
