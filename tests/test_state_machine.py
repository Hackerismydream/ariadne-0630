"""Tests for the task state machine — legal/illegal transitions, atomic claim,
retry chain, failure classification, stale claim recovery.

Per docs/architecture/task-state-machine.md "Tests Required".
"""

import threading

import pytest

from ariadne.models import AssigneeType, FailureReason, TaskStatus
from ariadne.store import (
    InvalidStateTransition,
    MaxAttemptsExhausted,
    Store,
)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def queued_task(store):
    """A task in queued status, ready for lifecycle testing."""
    issue = store.create_issue("test", "", AssigneeType.AGENT, "agent-1")
    agent = store.create_agent("A", "", ["codex"], [])
    return store.enqueue_task(issue.id, agent.id)


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------


def test_legal_transitions_completed(store, queued_task):
    """queued → claimed → running → completed"""
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    assert claimed is not None
    assert claimed.status == TaskStatus.CLAIMED

    running = store.start_task(claimed.id)
    assert running.status == TaskStatus.RUNNING

    completed = store.complete_task(claimed.id, {"output": "done"})
    assert completed.status == TaskStatus.COMPLETED
    assert completed.result == {"output": "done"}
    assert completed.completed_at is not None


def test_legal_transitions_failed(store, queued_task):
    """queued → claimed → running → failed"""
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    store.start_task(claimed.id)
    failed = store.fail_task(
        claimed.id, "codex crashed", FailureReason.AGENT_ERROR
    )
    assert failed.status == TaskStatus.FAILED
    assert failed.error == "codex crashed"
    assert failed.failure_reason == FailureReason.AGENT_ERROR
    assert failed.completed_at is not None


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


def test_illegal_transition_completed_to_running(store, queued_task):
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    store.start_task(claimed.id)
    store.complete_task(claimed.id, {})

    with pytest.raises(InvalidStateTransition):
        store.start_task(claimed.id)


def test_illegal_transition_failed_to_completed(store, queued_task):
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    store.start_task(claimed.id)
    store.fail_task(claimed.id, "err", FailureReason.AGENT_ERROR)

    with pytest.raises(InvalidStateTransition):
        store.complete_task(claimed.id, {})


def test_illegal_transition_queued_to_running(store, queued_task):
    """Cannot skip claimed — queued → running is illegal."""
    with pytest.raises(InvalidStateTransition):
        store.start_task(queued_task.id)


# ---------------------------------------------------------------------------
# Atomic claim
# ---------------------------------------------------------------------------


def test_atomic_claim(store, queued_task):
    """Two concurrent claim_task calls → only one gets the task."""
    results: list = []
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        r = store.claim_task(queued_task.agent_id, "rt-race")
        results.append(r)

    t1 = threading.Thread(target=claim)
    t2 = threading.Thread(target=claim)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, f"expected exactly 1 claim, got {len(claimed)}"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_creates_new_task(store, queued_task):
    """retry creates a new task with parent_task_id set, attempt incremented."""
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    store.start_task(claimed.id)
    store.fail_task(claimed.id, "err", FailureReason.AGENT_ERROR)

    retried = store.retry_task(claimed.id)
    assert retried.status == TaskStatus.QUEUED
    assert retried.attempt == claimed.attempt + 1
    assert retried.parent_task_id == claimed.id
    assert retried.id != claimed.id
    assert retried.issue_id == claimed.issue_id
    assert retried.agent_id == claimed.agent_id


def test_max_attempts_exhausted(store, queued_task):
    """After max_attempts failures, retry raises MaxAttemptsExhausted."""
    # First attempt fails
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    store.start_task(claimed.id)
    store.fail_task(claimed.id, "err", FailureReason.AGENT_ERROR)

    # Retry (attempt 2) — max_attempts is 2, so this is the last allowed
    retried = store.retry_task(claimed.id)
    assert retried.attempt == 2

    # Second attempt also fails
    claimed2 = store.claim_task(queued_task.agent_id, "rt-2")
    assert claimed2.id == retried.id
    store.start_task(claimed2.id)
    store.fail_task(claimed2.id, "err", FailureReason.AGENT_ERROR)

    # Now retry should fail — attempt 2 == max_attempts 2
    with pytest.raises(MaxAttemptsExhausted):
        store.retry_task(claimed2.id)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_failure_classification(store, queued_task):
    """Each FailureReason is stored and retrieved correctly."""
    reasons = [
        FailureReason.AGENT_ERROR,
        FailureReason.TIMEOUT,
        FailureReason.RUNTIME_OFFLINE,
        FailureReason.RUNTIME_RECOVERY,
        FailureReason.MANUAL,
    ]
    for reason in reasons:
        issue = store.create_issue(f"test-{reason.value}", "", AssigneeType.AGENT, "agent-1")
        store.enqueue_task(issue.id, "agent-1")
        claimed = store.claim_task("agent-1", "rt-test")
        store.start_task(claimed.id)
        failed = store.fail_task(claimed.id, f"err-{reason.value}", reason)
        assert failed.failure_reason == reason
        fetched = store.get_task(failed.id)
        assert fetched.failure_reason == reason


# ---------------------------------------------------------------------------
# Stale claim recovery
# ---------------------------------------------------------------------------


def test_stale_claim_recovery(store, queued_task):
    """A claimed task with an old dispatched_at is recovered to queued."""
    claimed = store.claim_task(queued_task.agent_id, "rt-1")
    assert claimed.status == TaskStatus.CLAIMED

    # Simulate stale by manually backdating dispatched_at
    from datetime import datetime, timedelta, timezone

    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    store._conn.execute(
        "UPDATE task SET dispatched_at = ? WHERE id = ?",
        (old_time, claimed.id),
    )
    store._conn.commit()

    recovered = store.recover_stale_claims(stale_timeout_seconds=60.0)
    assert recovered == 1

    task = store.get_task(claimed.id)
    assert task.status == TaskStatus.QUEUED
    assert task.failure_reason == FailureReason.RUNTIME_RECOVERY
