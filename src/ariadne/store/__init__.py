"""SQLite store facade.

The public `Store` API stays import-compatible while implementation moves into
lightweight repository and service layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ariadne.models import (
    Agent,
    AgentProfile,
    AgentProfileStatus,
    AssigneeType,
    FailureReason,
    Issue,
    IssueStatus,
    IssueTimelineEvent,
    LeaderDecision,
    LeaderDecisionOutcome,
    RuntimeCapability,
    RuntimeCapabilityStatus,
    RuntimeLease,
    RuntimeLeaseStatus,
    RuntimeMachine,
    RuntimeMachineStatus,
    Skill,
    Squad,
    SquadMember,
    Task,
    TaskRun,
    TaskRunClaim,
    TaskStatus,
)

from .benchmark_repo import BenchmarkRepo
from .base import (
    DEFAULT_AGENT_PROFILE_MAX_CONCURRENT_TASKRUNS,
    DEFAULT_RUNTIME_MAX_CONCURRENT_TASKRUNS,
    _ACTIVE_TASK_STATUS_SQL,
    _new_id,
    _now_iso,
    InvalidStateTransition,
    MaxAttemptsExhausted,
    StoreBase,
)


class Store(BenchmarkRepo, StoreBase):
    """Backward-compatible facade over the lightweight store layers."""

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

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    def append_issue_timeline_event(
        self,
        issue_id: str,
        event_type: str,
        actor_type: str = "system",
        actor_id: str | None = None,
        taskrun_id: str | None = None,
        runtime_lease_id: str | None = None,
        leader_decision_id: str | None = None,
        comment_id: str | None = None,
        payload: dict | None = None,
    ) -> IssueTimelineEvent:
        event_id = _new_id("evt")
        created_at = _now_iso()
        self._conn.execute(
            """INSERT INTO issue_timeline_event
               (id, issue_id, event_type, actor_type, actor_id, taskrun_id,
                runtime_lease_id, leader_decision_id, comment_id, payload_json,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                issue_id,
                event_type,
                actor_type,
                actor_id,
                taskrun_id,
                runtime_lease_id,
                leader_decision_id,
                comment_id,
                json.dumps(payload or {}),
                created_at,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM issue_timeline_event WHERE id = ?", (event_id,)
        ).fetchone()
        return self.row_to(IssueTimelineEvent, row)

    def get_issue_timeline(self, issue_id: str) -> list[IssueTimelineEvent]:
        rows = self._conn.execute(
            """SELECT * FROM issue_timeline_event
               WHERE issue_id = ?
               ORDER BY created_at, id""",
            (issue_id,),
        ).fetchall()
        return [self.row_to(IssueTimelineEvent, r) for r in rows]

    def record_leader_decision(
        self,
        issue_id: str,
        squad_id: str,
        leader_task_id: str,
        outcome: LeaderDecisionOutcome,
        reason: str = "",
        delegation_payload: dict | None = None,
        created_taskrun_ids: list[str] | None = None,
    ) -> LeaderDecision:
        decision_id = _new_id("leaderdecision")
        created_at = _now_iso()
        self._conn.execute(
            """INSERT INTO leader_decision
               (id, issue_id, squad_id, leader_task_id, outcome, reason,
                delegation_payload_json, created_taskrun_ids_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                issue_id,
                squad_id,
                leader_task_id,
                outcome.value,
                reason,
                json.dumps(delegation_payload or {}),
                json.dumps(created_taskrun_ids or []),
                created_at,
            ),
        )
        self._conn.commit()
        decision = self.get_leader_decision(decision_id)
        if decision is None:
            raise KeyError(f"leader decision not found: {decision_id}")
        self.append_issue_timeline_event(
            issue_id,
            "leader_decided",
            actor_type="leader",
            taskrun_id=leader_task_id,
            leader_decision_id=decision.id,
            payload={
                "outcome": outcome.value,
                "reason": reason,
                "delegation_payload": delegation_payload or {},
                "created_taskrun_ids": created_taskrun_ids or [],
            },
        )
        return decision

    def get_leader_decision(self, decision_id: str) -> LeaderDecision | None:
        row = self._conn.execute(
            "SELECT * FROM leader_decision WHERE id = ?", (decision_id,)
        ).fetchone()
        return self.row_to(LeaderDecision, row) if row else None

    def list_leader_decisions(self, issue_id: str | None = None) -> list[LeaderDecision]:
        if issue_id is None:
            rows = self._conn.execute(
                "SELECT * FROM leader_decision ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM leader_decision
                   WHERE issue_id = ?
                   ORDER BY created_at, id""",
                (issue_id,),
            ).fetchall()
        return [self.row_to(LeaderDecision, r) for r in rows]

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
        self.append_issue_timeline_event(
            issue.id,
            "issue_created",
            actor_type="user",
            payload={"title": issue.title},
        )
        return issue

    def get_issue(self, issue_id: str) -> Issue | None:
        row = self._conn.execute(
            "SELECT * FROM issue WHERE id = ?", (issue_id,)
        ).fetchone()
        return self.row_to(Issue, row) if row else None

    def list_issues(self) -> list[Issue]:
        rows = self._conn.execute("SELECT * FROM issue ORDER BY created_at").fetchall()
        return [self.row_to(Issue, r) for r in rows]

    def update_issue_status(self, issue_id: str, status: IssueStatus) -> Issue:
        self._conn.execute(
            "UPDATE issue SET status = ? WHERE id = ?",
            (status.value, issue_id),
        )
        self._conn.commit()
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(f"issue not found: {issue_id}")
        if status in (IssueStatus.DONE, IssueStatus.CANCELLED):
            self.append_issue_timeline_event(
                issue_id,
                "issue_closed",
                payload={"status": status.value},
            )
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
    # AgentProfile / Skill / Agent
    # ------------------------------------------------------------------

    def create_agent_profile(
        self,
        name: str,
        description: str = "",
        instructions: str = "",
        preferred_capabilities: list[str] | None = None,
        runtime_policy: dict | None = None,
        max_concurrent_taskruns: int = DEFAULT_AGENT_PROFILE_MAX_CONCURRENT_TASKRUNS,
        status: AgentProfileStatus = AgentProfileStatus.ACTIVE,
    ) -> AgentProfile:
        now = _now_iso()
        profile = AgentProfile(
            id=_new_id("profile"),
            name=name,
            description=description,
            instructions=instructions,
            preferred_capabilities=preferred_capabilities or [],
            runtime_policy=runtime_policy or {},
            max_concurrent_taskruns=max_concurrent_taskruns,
            status=status,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )
        self._conn.execute(
            """INSERT INTO agent_profile
               (id, name, description, instructions, preferred_capabilities_json,
                runtime_policy_json, max_concurrent_taskruns, status, created_at,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                profile.description,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                json.dumps(profile.runtime_policy),
                profile.max_concurrent_taskruns,
                profile.status.value,
                profile.created_at.isoformat(),
                profile.updated_at.isoformat(),
            ),
        )
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                "[]",
            ),
        )
        self._conn.commit()
        return profile

    def get_agent_profile(self, agent_profile_id: str) -> AgentProfile | None:
        row = self._conn.execute(
            "SELECT * FROM agent_profile WHERE id = ?", (agent_profile_id,)
        ).fetchone()
        return self.row_to(AgentProfile, row) if row else None

    def list_agent_profiles(self) -> list[AgentProfile]:
        rows = self._conn.execute(
            "SELECT * FROM agent_profile ORDER BY name"
        ).fetchall()
        return [self.row_to(AgentProfile, r) for r in rows]

    def create_skill(
        self,
        name: str,
        description: str = "",
        when_to_use: str = "",
        prompt_snippet: str = "",
        tools_allowed: list[str] | None = None,
        test_command: str | None = None,
        source_path: str | None = None,
        version: str = "",
    ) -> Skill:
        now = _now_iso()
        skill = Skill(
            id=_new_id("skill"),
            name=name,
            description=description,
            when_to_use=when_to_use,
            prompt_snippet=prompt_snippet,
            tools_allowed=tools_allowed or [],
            test_command=test_command,
            source_path=source_path,
            version=version,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )
        self._conn.execute(
            """INSERT INTO skill
               (id, name, description, when_to_use, prompt_snippet,
                tools_allowed_json, test_command, source_path, version,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                skill.id,
                skill.name,
                skill.description,
                skill.when_to_use,
                skill.prompt_snippet,
                json.dumps(skill.tools_allowed),
                skill.test_command,
                skill.source_path,
                skill.version,
                skill.created_at.isoformat(),
                skill.updated_at.isoformat(),
            ),
        )
        self._conn.commit()
        return skill

    def get_skill(self, skill_id: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT * FROM skill WHERE id = ?", (skill_id,)
        ).fetchone()
        return self.row_to(Skill, row) if row else None

    def get_skill_by_name(self, name: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT * FROM skill WHERE name = ?", (name,)
        ).fetchone()
        return self.row_to(Skill, row) if row else None

    def resolve_skill(self, skill_id_or_name: str) -> Skill | None:
        return self.get_skill(skill_id_or_name) or self.get_skill_by_name(skill_id_or_name)

    def list_skills(self) -> list[Skill]:
        rows = self._conn.execute("SELECT * FROM skill ORDER BY name").fetchall()
        return [self.row_to(Skill, r) for r in rows]

    def bind_skill_to_agent_profile(
        self, agent_profile_id: str, skill_id_or_name: str
    ) -> Skill:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise KeyError(f"agent profile not found: {agent_profile_id}")
        skill = self.resolve_skill(skill_id_or_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_id_or_name}")
        self._conn.execute(
            """INSERT OR IGNORE INTO agent_profile_skill
               (agent_profile_id, skill_id, created_at)
               VALUES (?, ?, ?)""",
            (agent_profile_id, skill.id, _now_iso()),
        )
        self._conn.commit()
        self._sync_legacy_agent_from_profile(agent_profile_id)
        return skill

    def list_skills_for_agent_profile(self, agent_profile_id: str) -> list[Skill]:
        rows = self._conn.execute(
            """SELECT skill.*
               FROM skill
               JOIN agent_profile_skill ON agent_profile_skill.skill_id = skill.id
               WHERE agent_profile_skill.agent_profile_id = ?
               ORDER BY skill.name""",
            (agent_profile_id,),
        ).fetchall()
        return [self.row_to(Skill, r) for r in rows]

    def _sync_legacy_agent_from_profile(self, agent_profile_id: str) -> None:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise KeyError(f"agent profile not found: {agent_profile_id}")
        skill_names = [skill.name for skill in self.list_skills_for_agent_profile(profile.id)]
        self._conn.execute(
            """INSERT INTO agent (id, name, instructions, backends, skills)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    instructions = excluded.instructions,
                    backends = excluded.backends,
                    skills = excluded.skills""",
            (
                profile.id,
                profile.name,
                profile.instructions,
                json.dumps(profile.preferred_capabilities),
                json.dumps(skill_names),
            ),
        )
        self._conn.commit()

    def _compose_taskrun_handoff(
        self, agent_profile_id: str, handoff_prompt: str | None
    ) -> str | None:
        skills = self.list_skills_for_agent_profile(agent_profile_id)
        blocks = []
        for skill in skills:
            lines = [f"### {skill.name}"]
            if skill.description:
                lines.append(f"Routing description: {skill.description}")
            if skill.when_to_use:
                lines.append(f"When to use: {skill.when_to_use}")
            if skill.prompt_snippet:
                lines.append(f"Prompt content: {skill.prompt_snippet}")
            if skill.tools_allowed:
                lines.append(f"Allowed tools: {', '.join(skill.tools_allowed)}")
            if skill.test_command:
                lines.append(f"Verification command: {skill.test_command}")
            blocks.append("\n".join(lines))
        if not blocks:
            return handoff_prompt
        section = "Skill capability package:\n" + "\n\n".join(blocks)
        if not handoff_prompt:
            return section
        return f"{handoff_prompt}\n\n{section}"

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
        return self.row_to(Agent, row) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute("SELECT * FROM agent ORDER BY name").fetchall()
        return [self.row_to(Agent, r) for r in rows]

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
