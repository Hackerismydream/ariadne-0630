"""RuntimeLease claim, heartbeat, expiry, and daemon integration tests."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne import models
from ariadne.models import (
    AssigneeType,
    RuntimeCapabilityStatus,
    TaskStatus,
)
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def seed_runtime(store: Store, tmp_path, runtime_id: str = "rt-lease"):
    store.register_runtime_machine(
        runtime_machine_id=runtime_id,
        name="Lease Runtime",
        workspace_root=str(tmp_path),
    )
    return store.upsert_runtime_capability(
        runtime_machine_id=runtime_id,
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )


def seed_taskrun(store: Store):
    agent = store.create_agent("Runner", "", ["dry-run"], [])
    issue = store.create_issue("Lease work", "", AssigneeType.AGENT, agent.id)
    return store.enqueue_taskrun(issue.id, agent.id)


def test_runtime_machine_default_capacity_is_parallel(store: Store, tmp_path):
    machine = store.register_runtime_machine(
        runtime_machine_id="rt-default",
        name="Default Runtime",
        workspace_root=str(tmp_path),
    )

    assert machine.max_concurrent_taskruns == 4


def test_claim_taskrun_for_runtime_machine_creates_active_lease(store: Store, tmp_path):
    assert hasattr(models, "RuntimeLeaseStatus")
    RuntimeLeaseStatus = models.RuntimeLeaseStatus

    capability = seed_runtime(store, tmp_path)
    taskrun = seed_taskrun(store)

    claim = store.claim_taskrun_for_runtime_machine("rt-lease", lease_seconds=30)

    assert claim is not None
    assert claim.taskrun.id == taskrun.id
    assert claim.taskrun.status == TaskStatus.PREPARING
    assert claim.lease.status == RuntimeLeaseStatus.ACTIVE
    assert claim.lease.runtime_capability_id == capability.id
    assert claim.lease.lease_token

    started = store.start_taskrun(taskrun.id)
    completed = store.complete_taskrun(started.id, {"ok": True})
    released = store.release_runtime_lease(claim.lease.id)

    assert completed.status == TaskStatus.COMPLETED
    assert released.status == RuntimeLeaseStatus.RELEASED
    assert released.released_at is not None


def test_expire_runtime_leases_marks_taskrun_failed(store: Store, tmp_path):
    assert hasattr(models, "RuntimeLeaseStatus")
    RuntimeLeaseStatus = models.RuntimeLeaseStatus

    seed_runtime(store, tmp_path)
    taskrun = seed_taskrun(store)
    claim = store.claim_taskrun_for_runtime_machine("rt-lease", lease_seconds=-1)
    assert claim is not None

    expired = store.expire_runtime_leases()

    assert [lease.id for lease in expired] == [claim.lease.id]
    lease = store.get_runtime_lease(claim.lease.id)
    assert lease is not None
    assert lease.status == RuntimeLeaseStatus.EXPIRED
    failed = store.get_taskrun(taskrun.id)
    assert failed is not None
    assert failed.status == TaskStatus.FAILED
    assert failed.failure_reason.value == "runtime_offline"


def test_cancel_taskrun_revokes_active_runtime_lease(store: Store, tmp_path):
    assert hasattr(models, "RuntimeLeaseStatus")
    RuntimeLeaseStatus = models.RuntimeLeaseStatus

    seed_runtime(store, tmp_path)
    taskrun = seed_taskrun(store)
    claim = store.claim_taskrun_for_runtime_machine("rt-lease")
    assert claim is not None

    cancelled = store.cancel_taskrun(taskrun.id)
    lease = store.get_runtime_lease(claim.lease.id)

    assert cancelled.status == TaskStatus.CANCELLED
    assert lease is not None
    assert lease.status == RuntimeLeaseStatus.REVOKED
    assert lease.revoke_reason == "taskrun_cancelled"


def test_concurrent_claims_create_only_one_active_runtime_lease(tmp_path):
    assert hasattr(models, "RuntimeLeaseStatus")
    RuntimeLeaseStatus = models.RuntimeLeaseStatus

    db = str(tmp_path / "claims.db")
    setup = Store(db)
    seed_runtime(setup, tmp_path)
    taskrun = seed_taskrun(setup)
    setup.close()

    def claim_once():
        s = Store(db)
        try:
            claim = s.claim_taskrun_for_runtime_machine("rt-lease", lease_seconds=30)
            return claim.lease.id if claim else None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim_once(), range(2)))

    verify = Store(db)
    try:
        assert len([r for r in results if r is not None]) == 1
        leases = verify.list_runtime_leases(taskrun.id)
        assert len(
            [lease for lease in leases if lease.status == RuntimeLeaseStatus.ACTIVE]
        ) == 1
    finally:
        verify.close()


def test_runtime_machine_claim_serializes_active_taskruns_per_issue(
    store: Store, tmp_path
):
    seed_runtime(store, tmp_path)
    agent = store.create_agent("Runner", "", ["dry-run"], [])
    issue = store.create_issue("same issue", "", AssigneeType.AGENT, agent.id)
    first = store.enqueue_taskrun(issue.id, agent.id)
    second = store.enqueue_taskrun(issue.id, agent.id)

    first_claim = store.claim_taskrun_for_runtime_machine("rt-lease")
    second_claim = store.claim_taskrun_for_runtime_machine("rt-lease")

    assert first_claim is not None
    assert first_claim.taskrun.id == first.id
    assert second_claim is None
    assert store.get_taskrun(second.id).status == TaskStatus.QUEUED

    store.start_taskrun(first_claim.taskrun.id)
    store.complete_taskrun(first_claim.taskrun.id, {"ok": True})
    store.release_runtime_lease(first_claim.lease.id)
    second_claim_after_terminal = store.claim_taskrun_for_runtime_machine("rt-lease")

    assert second_claim_after_terminal is not None
    assert second_claim_after_terminal.taskrun.id == second.id


def test_runtime_machine_claim_respects_runtime_capacity(store: Store, tmp_path):
    store.register_runtime_machine(
        runtime_machine_id="rt-capacity",
        name="Capacity Runtime",
        workspace_root=str(tmp_path),
        max_concurrent_taskruns=4,
    )
    store.upsert_runtime_capability(
        runtime_machine_id="rt-capacity",
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )
    profile = store.create_agent_profile(
        "Parallel Runner",
        preferred_capabilities=["dry-run"],
        max_concurrent_taskruns=4,
    )
    for index in range(5):
        issue = store.create_issue(
            f"issue {index}", "", AssigneeType.AGENT, profile.id
        )
        store.enqueue_taskrun(issue.id, profile.id)

    claims = [
        store.claim_taskrun_for_runtime_machine("rt-capacity")
        for _ in range(5)
    ]

    assert len([claim for claim in claims if claim is not None]) == 4
    assert claims[-1] is None


def test_runtime_machine_does_not_claim_unavailable_backend_taskrun(
    store: Store,
    tmp_path,
):
    store.register_runtime_machine(
        runtime_machine_id="rt-dry-only",
        name="Dry Runtime",
        workspace_root=str(tmp_path),
    )
    store.upsert_runtime_capability(
        runtime_machine_id="rt-dry-only",
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )
    agent = store.create_agent("Codex Runner", "", ["codex"], [])
    issue = store.create_issue("needs codex", "", AssigneeType.AGENT, agent.id)
    taskrun = store.enqueue_taskrun(issue.id, agent.id)

    claim = store.claim_taskrun_for_runtime_machine("rt-dry-only")

    assert claim is None
    assert store.get_taskrun(taskrun.id).status == TaskStatus.QUEUED


def test_runtime_machine_claim_respects_agent_profile_capacity(
    store: Store, tmp_path
):
    store.register_runtime_machine(
        runtime_machine_id="rt-profile-capacity",
        name="Profile Capacity Runtime",
        workspace_root=str(tmp_path),
        max_concurrent_taskruns=4,
    )
    store.upsert_runtime_capability(
        runtime_machine_id="rt-profile-capacity",
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )
    profile = store.create_agent_profile(
        "Serial Runner",
        preferred_capabilities=["dry-run"],
        max_concurrent_taskruns=1,
    )
    first_issue = store.create_issue("first", "", AssigneeType.AGENT, profile.id)
    second_issue = store.create_issue("second", "", AssigneeType.AGENT, profile.id)
    first = store.enqueue_taskrun(first_issue.id, profile.id)
    second = store.enqueue_taskrun(second_issue.id, profile.id)

    first_claim = store.claim_taskrun_for_runtime_machine("rt-profile-capacity")
    second_claim = store.claim_taskrun_for_runtime_machine("rt-profile-capacity")

    assert first_claim is not None
    assert first_claim.taskrun.id == first.id
    assert second_claim is None
    assert store.get_taskrun(second.id).status == TaskStatus.QUEUED

    store.start_taskrun(first_claim.taskrun.id)
    store.complete_taskrun(first_claim.taskrun.id, {"ok": True})
    store.release_runtime_lease(first_claim.lease.id)
    second_claim_after_terminal = store.claim_taskrun_for_runtime_machine(
        "rt-profile-capacity"
    )

    assert second_claim_after_terminal is not None
    assert second_claim_after_terminal.taskrun.id == second.id


def test_daemon_executes_dry_run_taskrun_through_runtime_lease(store: Store, tmp_path):
    assert hasattr(models, "RuntimeLeaseStatus")
    RuntimeLeaseStatus = models.RuntimeLeaseStatus

    taskrun = seed_taskrun(store)
    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="rt-daemon",
        poll_interval=0.01,
        target_repo_path=str(tmp_path),
    )

    daemon.start(max_iterations=1)

    completed = store.get_taskrun(taskrun.id)
    assert completed is not None
    assert completed.status == TaskStatus.COMPLETED
    leases = store.list_runtime_leases(taskrun.id)
    assert len(leases) == 1
    assert leases[0].status == RuntimeLeaseStatus.RELEASED
