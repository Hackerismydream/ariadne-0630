"""Layered execution policy gate.

The backend owns workspace isolation and the explicit write-workspace escape
hatch. This module is the control-plane gate: it decides whether a TaskRun is
allowed to reach a real coding-agent backend and names the layer that blocked
it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ariadne.models import (
    ExecutionContext,
    ExecutionPolicyDecision,
    ExecutionPolicyLayer,
    RuntimeCapabilityStatus,
    RuntimeLeaseStatus,
    Task,
)
from ariadne.store import Store

REAL_EXECUTION_BACKENDS = {"codex", "claude-code"}


def evaluate_execution_policy(
    store: Store,
    task: Task,
    context: ExecutionContext,
    backend_name: str,
    runtime_id: str,
) -> ExecutionPolicyDecision:
    """Evaluate all policy layers before real backend execution.

    Dry-run is intentionally kept available: it is the safe simulation path and
    should not require real-execution policy grants.
    """
    if backend_name == "dry-run":
        return ExecutionPolicyDecision(
            allowed=True,
            details={"dry_run": True},
        )
    if backend_name not in REAL_EXECUTION_BACKENDS:
        return ExecutionPolicyDecision(
            allowed=True,
            details={"custom_backend": backend_name},
        )

    lease = store.get_active_runtime_lease_for_taskrun(task.id)
    if lease is None:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_LEASE,
            "real execution requires an active RuntimeLease",
            taskrun_id=task.id,
        )
    now = datetime.now(timezone.utc)
    if lease.status != RuntimeLeaseStatus.ACTIVE or lease.expires_at <= now:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_LEASE,
            "RuntimeLease is not fresh",
            taskrun_id=task.id,
            lease_id=lease.id,
            lease_status=lease.status.value,
            expires_at=lease.expires_at.isoformat(),
        )
    if lease.runtime_machine_id != runtime_id:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_LEASE,
            "RuntimeLease belongs to a different RuntimeMachine",
            lease_runtime_machine_id=lease.runtime_machine_id,
            runtime_id=runtime_id,
        )

    machine = store.get_runtime_machine(runtime_id)
    if machine is None:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_MACHINE,
            "RuntimeMachine is not registered",
            runtime_id=runtime_id,
        )
    target_repo = _resolve(context.target_repo_path)
    if machine.workspace_root and not _is_within(target_repo, _resolve(machine.workspace_root)):
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_MACHINE,
            "target repo is outside RuntimeMachine workspace root",
            target_repo_path=str(target_repo),
            workspace_root=machine.workspace_root,
        )
    allowlist = [_resolve(path) for path in machine.repo_allowlist if path]
    if allowlist and not any(_is_within(target_repo, allowed) for allowed in allowlist):
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_MACHINE,
            "target repo is outside RuntimeMachine repository allowlist",
            target_repo_path=str(target_repo),
            repo_allowlist=machine.repo_allowlist,
        )
    active_leases = [
        candidate
        for candidate in store.list_runtime_leases()
        if candidate.runtime_machine_id == runtime_id
        and candidate.status == RuntimeLeaseStatus.ACTIVE
    ]
    if len(active_leases) > machine.max_concurrent_taskruns:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_MACHINE,
            "RuntimeMachine process limit exceeded",
            active_leases=len(active_leases),
            max_concurrent_taskruns=machine.max_concurrent_taskruns,
        )
    if machine.metadata.get("allow_shell") is False:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_MACHINE,
            "RuntimeMachine shell execution is disabled",
            runtime_id=runtime_id,
        )

    capability = store.get_runtime_capability(lease.runtime_capability_id)
    if capability is None:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_CAPABILITY,
            "RuntimeCapability from lease is missing",
            capability_id=lease.runtime_capability_id,
        )
    if capability.status != RuntimeCapabilityStatus.AVAILABLE:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_CAPABILITY,
            "RuntimeCapability is not available",
            capability_id=capability.id,
            status=capability.status.value,
        )
    if capability.provider != backend_name:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_CAPABILITY,
            "RuntimeCapability provider does not match requested backend",
            capability_provider=capability.provider,
            backend_name=backend_name,
        )
    if not capability.command_path:
        return _blocked(
            ExecutionPolicyLayer.RUNTIME_CAPABILITY,
            "RuntimeCapability command path is empty",
            capability_id=capability.id,
        )

    profile = store.get_agent_profile(task.agent_id)
    bound_skills = store.list_skills_for_agent_profile(task.agent_id)
    if profile is not None:
        if profile.status.value != "active":
            return _blocked(
                ExecutionPolicyLayer.AGENT_PROFILE,
                "AgentProfile is not active",
                agent_profile_id=profile.id,
                status=profile.status.value,
            )
        if backend_name not in profile.preferred_capabilities:
            return _blocked(
                ExecutionPolicyLayer.AGENT_PROFILE,
                "backend is not in AgentProfile preferred capabilities",
                backend_name=backend_name,
                preferred_capabilities=profile.preferred_capabilities,
            )
        if not profile.runtime_policy.get("allow_real_execution", False):
            return _blocked(
                ExecutionPolicyLayer.AGENT_PROFILE,
                "AgentProfile policy does not allow real execution",
                agent_profile_id=profile.id,
            )
        active_for_profile = _active_leases_for_agent_profile(store, profile.id)
        if active_for_profile > profile.max_concurrent_taskruns:
            return _blocked(
                ExecutionPolicyLayer.AGENT_PROFILE,
                "AgentProfile concurrency limit exceeded",
                active_taskruns=active_for_profile,
                max_concurrent_taskruns=profile.max_concurrent_taskruns,
            )
        required_skills = profile.runtime_policy.get("required_skills", [])
        bound_skill_names = {skill.name for skill in bound_skills}
        missing_skills = [
            skill for skill in required_skills if skill not in bound_skill_names
        ]
        if missing_skills:
            return _blocked(
                ExecutionPolicyLayer.AGENT_PROFILE,
                "AgentProfile required skills are not bound",
                missing_skills=missing_skills,
            )

    for skill in bound_skills:
        if skill.tools_allowed and backend_name not in skill.tools_allowed:
            return _blocked(
                ExecutionPolicyLayer.SKILL,
                "Skill policy does not allow requested backend",
                skill_id=skill.id,
                skill_name=skill.name,
                backend_name=backend_name,
                tools_allowed=skill.tools_allowed,
            )

    if context.timeout_seconds < 1 or context.timeout_seconds > 7200:
        return _blocked(
            ExecutionPolicyLayer.TASKRUN,
            "TaskRun timeout is outside allowed bounds",
            timeout_seconds=context.timeout_seconds,
        )
    if not target_repo.exists():
        return _blocked(
            ExecutionPolicyLayer.TASKRUN,
            "target repo path does not exist",
            target_repo_path=str(target_repo),
        )
    if profile is not None and profile.runtime_policy.get("redact_secrets", True) is False:
        return _blocked(
            ExecutionPolicyLayer.TASKRUN,
            "TaskRun redaction policy is disabled",
            agent_profile_id=profile.id,
        )

    return ExecutionPolicyDecision(
        allowed=True,
        details={
            "backend_name": backend_name,
            "runtime_machine_id": runtime_id,
            "runtime_capability_id": capability.id,
            "runtime_lease_id": lease.id,
        },
    )


def _blocked(
    layer: ExecutionPolicyLayer,
    reason: str,
    **details: object,
) -> ExecutionPolicyDecision:
    return ExecutionPolicyDecision(
        allowed=False,
        layer=layer,
        reason=reason,
        details=dict(details),
    )


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _active_leases_for_agent_profile(store: Store, agent_profile_id: str) -> int:
    row = store._conn.execute(
        """SELECT COUNT(*) AS count
           FROM runtime_lease
           JOIN task ON task.id = runtime_lease.taskrun_id
           WHERE runtime_lease.status = 'active'
             AND task.agent_id = ?""",
        (agent_profile_id,),
    ).fetchone()
    return int(row["count"])
