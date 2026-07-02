"""Tests for the task state machine — legal/illegal transitions, atomic claim,
retry chain, failure classification, stale claim recovery.

Per docs/architecture/task-state-machine.md "Tests Required".
"""

import sqlite3
import threading

import pytest

from ariadne.models import AssigneeType, FailureReason, IssueStatus, TaskStatus
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


def test_claim_task_serializes_active_tasks_per_issue(store):
    agent = store.create_agent("A", "", ["dry-run"], [])
    issue = store.create_issue("same issue", "", AssigneeType.AGENT, agent.id)
    first = store.enqueue_task(issue.id, agent.id)
    second = store.enqueue_task(issue.id, agent.id)

    claimed_first = store.claim_task(agent.id, "rt-1")
    claimed_second = store.claim_task(agent.id, "rt-2")

    assert claimed_first is not None
    assert claimed_first.id == first.id
    assert claimed_second is None
    assert store.get_task(second.id).status == TaskStatus.QUEUED

    store.start_task(claimed_first.id)
    store.complete_task(claimed_first.id, {"ok": True})
    claimed_after_terminal = store.claim_task(agent.id, "rt-2")

    assert claimed_after_terminal is not None
    assert claimed_after_terminal.id == second.id


def test_claim_task_allows_parallel_active_tasks_for_different_issues(store):
    agent = store.create_agent("A", "", ["dry-run"], [])
    first_issue = store.create_issue("first issue", "", AssigneeType.AGENT, agent.id)
    second_issue = store.create_issue("second issue", "", AssigneeType.AGENT, agent.id)
    first = store.enqueue_task(first_issue.id, agent.id)
    second = store.enqueue_task(second_issue.id, agent.id)

    claimed_first = store.claim_task(agent.id, "rt-1")
    claimed_second = store.claim_task(agent.id, "rt-2")

    assert claimed_first is not None
    assert claimed_second is not None
    assert {claimed_first.id, claimed_second.id} == {first.id, second.id}


def test_task_migration_resolves_duplicate_active_tasks_per_issue(tmp_path, caplog):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE issue (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'backlog'
                CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'cancelled')),
            assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
            assignee_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE task (
            id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            squad_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'preparing', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
            attempt INTEGER NOT NULL DEFAULT 1,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
            failure_reason TEXT
                CHECK (failure_reason IS NULL OR failure_reason IN
                       ('agent_error', 'timeout', 'runtime_offline', 'runtime_recovery',
                        'manual', 'policy_blocked', 'provider_error', 'test_failure',
                        'routing_failure', 'llm_parse_failure')),
            dispatched_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            runtime_id TEXT,
            handoff_prompt TEXT,
            trace_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO issue
            (id, title, description, status, assignee_type, assignee_id, created_at)
            VALUES ('issue-1', 'legacy', '', 'todo', 'agent', 'agent-1',
                    '2026-01-01T00:00:00+00:00');
        INSERT INTO task
            (id, issue_id, agent_id, status, dispatched_at, started_at, created_at)
            VALUES
            ('task-1', 'issue-1', 'agent-1', 'claimed',
             '2026-01-01T00:00:00+00:00', NULL, '2026-01-01T00:00:00+00:00'),
            ('task-2', 'issue-1', 'agent-1', 'running',
             '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:02+00:00',
             '2026-01-01T00:00:01+00:00');
        """
    )
    conn.commit()
    conn.close()

    with caplog.at_level("WARNING"):
        migrated = Store(str(db_path))
    try:
        active_count = migrated._conn.execute(
            """SELECT COUNT(*) FROM task
               WHERE issue_id = 'issue-1'
                 AND status IN ('claimed', 'preparing', 'running')"""
        ).fetchone()[0]
        failed = migrated._conn.execute(
            "SELECT * FROM task WHERE id = 'task-2'"
        ).fetchone()
        index_row = migrated._conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'index' AND name = 'idx_task_one_active_per_issue'"""
        ).fetchone()
    finally:
        migrated.close()

    assert active_count == 1
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "runtime_recovery"
    assert index_row is not None
    assert "duplicate active tasks for one issue" in caplog.text


def test_issue_status_migration_allows_failed_status(tmp_path):
    db_path = tmp_path / "legacy-issue.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE issue (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'backlog'
                CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'cancelled')),
            assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
            assignee_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO issue
            (id, title, description, status, assignee_type, assignee_id, created_at)
            VALUES ('issue-1', 'legacy', '', 'todo', 'agent', 'agent-1',
                    '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    migrated = Store(str(db_path))
    try:
        updated = migrated.update_issue_status("issue-1", IssueStatus.FAILED)
        table_sql = migrated._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'issue'"
        ).fetchone()["sql"]
    finally:
        migrated.close()

    assert updated.status == IssueStatus.FAILED
    assert "'failed'" in table_sql


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
