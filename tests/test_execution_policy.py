"""Layered ExecutionPolicy gate tests."""

import sqlite3
from pathlib import Path

import pytest

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import (
    AssigneeType,
    ExecutionResult,
    FailureReason,
    RuntimeCapabilityStatus,
    TaskStatus,
)
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


class ExplodingBackend:
    name = "codex"
    executable_name = "codex"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def execute(self, ctx, on_progress=None):
        self.calls += 1
        raise AssertionError("policy-blocked backend must not execute")


class SuccessfulCodexBackend:
    name = "codex"
    executable_name = "codex"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def execute(self, ctx, on_progress=None):
        self.calls += 1
        return ExecutionResult(
            backend_name="codex",
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            diff=None,
            changed_files=[],
            failure_reason=None,
            duration_seconds=0.01,
            command="codex",
            command_cwd=ctx.target_repo_path,
            execution_repo_path=ctx.target_repo_path,
        )


def _register_real_runtime(store: Store, runtime_id: str, target: Path):
    store.register_runtime_machine(
        runtime_machine_id=runtime_id,
        name="policy-runtime",
        workspace_root=str(target),
        repo_allowlist=[str(target)],
        max_concurrent_taskruns=1,
    )
    return store.upsert_runtime_capability(
        runtime_machine_id=runtime_id,
        provider="codex",
        command_path="/usr/local/bin/codex",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )


def _seed_codex_taskrun(
    store: Store,
    runtime_policy: dict | None,
    target: Path,
    lease_seconds: int = 60,
):
    profile = store.create_agent_profile(
        name="Coder",
        instructions="Write code",
        preferred_capabilities=["codex"],
        runtime_policy=runtime_policy or {},
    )
    issue = store.create_issue("Real work", "Change code", AssigneeType.AGENT, profile.id)
    taskrun = store.enqueue_taskrun(issue.id, profile.id)
    claim = store.claim_taskrun_for_runtime_machine("rt-policy", lease_seconds=lease_seconds)
    assert claim is not None
    assert claim.taskrun.id == taskrun.id
    return profile, issue, claim.taskrun


def _execute_with_policy(store: Store, taskrun, target: Path, backend: ExplodingBackend):
    daemon = Daemon(
        store=store,
        backend_factory=lambda name: backend,
        runtime_id="rt-policy",
        target_repo_path=str(target),
    )
    daemon._runtime_registered = True
    daemon._execute_task(taskrun)


def _policy_event(store: Store, issue_id: str):
    events = store.get_issue_timeline(issue_id)
    return next(event for event in events if event.event_type == "execution_policy_blocked")


def test_dry_run_remains_available_without_real_execution_policy(store, tmp_path):
    profile = store.create_agent_profile(
        name="Dry Runner",
        preferred_capabilities=["dry-run"],
        runtime_policy={"allow_real_execution": False},
    )
    issue = store.create_issue("Dry work", "simulate", AssigneeType.AGENT, profile.id)
    taskrun = store.enqueue_taskrun(issue.id, profile.id)

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="rt-dry",
        target_repo_path=str(tmp_path),
    )
    daemon.start(max_iterations=1)

    completed = store.get_taskrun(taskrun.id)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.failure_reason is None


def test_agent_profile_policy_blocks_real_backend_without_reporting_success(store, tmp_path):
    backend = ExplodingBackend()
    _register_real_runtime(store, "rt-policy", tmp_path)
    profile, issue, taskrun = _seed_codex_taskrun(
        store,
        runtime_policy={"allow_real_execution": False},
        target=tmp_path,
    )

    _execute_with_policy(store, taskrun, tmp_path, backend)

    failed = store.get_taskrun(taskrun.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.failure_reason == FailureReason.POLICY_BLOCKED
    assert backend.calls == 0
    event = _policy_event(store, issue.id)
    assert event.payload["layer"] == "agent_profile"
    assert profile.id in event.payload["details"]["agent_profile_id"]


def test_runtime_machine_policy_blocks_repo_outside_allowlist(store, tmp_path):
    backend = ExplodingBackend()
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    _register_real_runtime(store, "rt-policy", allowed)
    _, issue, taskrun = _seed_codex_taskrun(
        store,
        runtime_policy={"allow_real_execution": True},
        target=allowed,
    )

    _execute_with_policy(store, taskrun, outside, backend)

    failed = store.get_taskrun(taskrun.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.failure_reason == FailureReason.POLICY_BLOCKED
    assert backend.calls == 0
    assert _policy_event(store, issue.id).payload["layer"] == "runtime_machine"


def test_runtime_lease_policy_blocks_expired_lease(store, tmp_path):
    backend = ExplodingBackend()
    _register_real_runtime(store, "rt-policy", tmp_path)
    _, issue, taskrun = _seed_codex_taskrun(
        store,
        runtime_policy={"allow_real_execution": True},
        target=tmp_path,
        lease_seconds=-1,
    )

    _execute_with_policy(store, taskrun, tmp_path, backend)

    failed = store.get_taskrun(taskrun.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.failure_reason == FailureReason.POLICY_BLOCKED
    assert backend.calls == 0
    assert _policy_event(store, issue.id).payload["layer"] == "runtime_lease"


def test_taskrun_policy_no_longer_requires_environment_or_confirmation_gate(store, tmp_path, monkeypatch):
    backend = SuccessfulCodexBackend()
    monkeypatch.delenv("ARIADNE_ENABLE_EXTERNAL_EXECUTION", raising=False)
    _register_real_runtime(store, "rt-policy", tmp_path)
    _, issue, taskrun = _seed_codex_taskrun(
        store,
        runtime_policy={"allow_real_execution": True},
        target=tmp_path,
    )

    _execute_with_policy(store, taskrun, tmp_path, backend)

    completed = store.get_taskrun(taskrun.id)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.failure_reason is None
    assert backend.calls == 1
    assert not [
        event
        for event in store.get_issue_timeline(issue.id)
        if event.event_type == "execution_policy_blocked"
    ]


def test_store_migrates_legacy_task_constraints(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
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
                CHECK (status IN ('queued', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
            attempt INTEGER NOT NULL DEFAULT 1,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
            failure_reason TEXT
                CHECK (failure_reason IS NULL OR failure_reason IN
                       ('agent_error', 'timeout', 'runtime_offline', 'runtime_recovery', 'manual')),
            dispatched_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            runtime_id TEXT,
            handoff_prompt TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    migrated = Store(str(db))
    try:
        table_sql = migrated._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task'"
        ).fetchone()["sql"]
        cols = [
            row[1]
            for row in migrated._conn.execute("PRAGMA table_info(task)").fetchall()
        ]

        assert "preparing" in table_sql
        assert "policy_blocked" in table_sql
        assert "trace_id" in cols
    finally:
        migrated.close()
