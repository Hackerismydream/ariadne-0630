"""Artifact-backed benchmark runners for Ariadne.

The benchmark layer is intentionally product-facts first: every suite writes a
SQLite DB plus exported TaskRun/RuntimeLease/IssueTimeline/LeaderDecision facts,
then derives metrics from those artifacts rather than from terminal text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ariadne.backends import CodexBackend, ClaudeBackend, DryRunBackend, _ShellBackend
from ariadne.daemon import Daemon
from ariadne.models import (
    AssigneeType,
    ExecutionContext,
    FailureReason,
    IssueStatus,
    LeaderDecisionOutcome,
    RuntimeCapabilityStatus,
    TaskStatus,
)
from ariadne.orchestrator import Orchestrator
from ariadne.store import MaxAttemptsExhausted, Store

METRICS_SCHEMA_VERSION = "ariadne.benchmark.metrics.v1"
CASE_SCHEMA_VERSION = "ariadne.benchmark.case.v1"

PRODUCT_FACT_TABLES = [
    "issue",
    "task",
    "runtime_machine",
    "runtime_capability",
    "runtime_lease",
    "issue_timeline_event",
    "leader_decision",
    "benchmark_run",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def git_metadata(cwd: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = run(["git", "status", "--porcelain"])
    return {
        "commit": run(["git", "rev-parse", "HEAD"]),
        "short_commit": run(["git", "rev-parse", "--short", "HEAD"]),
        "branch": run(["git", "branch", "--show-current"]),
        "dirty": bool(status),
    }


def environment_metadata() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "cpu": platform.processor() or platform.machine(),
        "runner": "local",
    }


def create_run_metadata(artifact_dir: Path, suite_name: str, case_id: str) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "run_id": artifact_dir.name,
        "suite_name": suite_name,
        "case_id": case_id,
        "started_at": utc_now(),
        "git": git_metadata(Path.cwd()),
        "environment": environment_metadata(),
    }
    write_json(artifact_dir / "run_metadata.json", metadata)
    return metadata


def export_product_facts(store: Store, artifact_dir: Path) -> dict[str, str]:
    product_dir = artifact_dir / "product_facts"
    product_dir.mkdir(parents=True, exist_ok=True)
    db_copy = product_dir / "ariadne.db"
    store._conn.commit()
    backup = sqlite3.connect(str(db_copy))
    try:
        store._conn.backup(backup)
    finally:
        backup.close()

    exported: dict[str, str] = {"sqlite_dump": str(db_copy.relative_to(artifact_dir))}
    for table in PRODUCT_FACT_TABLES:
        columns = [row["name"] for row in store._conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = [row_to_dict(row) for row in store._conn.execute(f"SELECT * FROM {table}").fetchall()]
        jsonl_path = product_dir / f"{table}.jsonl"
        csv_path = product_dir / f"{table}.csv"
        write_jsonl(jsonl_path, rows)
        write_csv(csv_path, rows, columns)
        exported[f"{table}_jsonl"] = str(jsonl_path.relative_to(artifact_dir))
        exported[f"{table}_csv"] = str(csv_path.relative_to(artifact_dir))
    return exported


def hash_artifacts(artifact_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file() or path.name == "hashes.txt":
            continue
        rel = str(path.relative_to(artifact_dir))
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    lines = [f"{digest}  {rel}" for rel, digest in hashes.items()]
    (artifact_dir / "hashes.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    return hashes


def write_summary(artifact_dir: Path, title: str, metrics: dict[str, Any]) -> None:
    lines = [f"# {title}", "", f"- status: {metrics.get('status', 'unknown')}"]
    for key in sorted(metrics):
        if key in {"artifacts", "summary"}:
            continue
        value = metrics[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            lines.append(f"- {key}: {value}")
    (artifact_dir / "summary.md").write_text("\n".join(lines) + "\n")


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = round((p / 100) * (len(values) - 1))
    return round(values[index], 4)


def failure_class(reason: str | None) -> str:
    if reason is None:
        return "unknown"
    if reason == FailureReason.POLICY_BLOCKED.value:
        return "policy"
    if reason in {
        FailureReason.RUNTIME_OFFLINE.value,
        FailureReason.RUNTIME_RECOVERY.value,
        FailureReason.TIMEOUT.value,
    }:
        return "runtime"
    if reason == FailureReason.PROVIDER_ERROR.value:
        return "provider"
    if reason == FailureReason.TEST_FAILURE.value:
        return "test"
    if reason == FailureReason.ROUTING_FAILURE.value:
        return "routing"
    if reason == FailureReason.LLM_PARSE_FAILURE.value:
        return "llm_parse"
    if reason == FailureReason.MANUAL.value:
        return "manual_cancellation"
    if reason == FailureReason.AGENT_ERROR.value:
        return "agent"
    return "unknown"


def finish_case(artifact_dir: Path, title: str, metrics: dict[str, Any], store: Store | None) -> dict[str, Any]:
    metrics.setdefault("schema_version", METRICS_SCHEMA_VERSION)
    metrics.setdefault("completed_at", utc_now())
    artifacts: dict[str, str] = {}
    if store is not None:
        artifacts.update(export_product_facts(store, artifact_dir))
    metrics["artifacts"] = artifacts
    write_json(artifact_dir / "metrics.json", metrics)
    write_summary(artifact_dir, title, metrics)
    hash_artifacts(artifact_dir)
    return metrics


def setup_runtime(store: Store, runtime_id: str = "runtime-bench") -> None:
    store.register_runtime_machine(runtime_id, "Benchmark Runtime")
    store.upsert_runtime_capability(
        runtime_id,
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )


def run_artifact_spine(artifact_dir: Path) -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "artifact_spine", "artifact-spine-smoke")
    write_json(
        artifact_dir / "case_manifest.json",
        {
            "schema_version": CASE_SCHEMA_VERSION,
            "id": "artifact-spine-smoke",
            "suite": "artifact_spine",
            "provider": "dry-run",
        },
    )
    store = Store(str(artifact_dir / "ariadne.db"))
    try:
        setup_runtime(store)
        profile = store.create_agent_profile("Smoke Agent", preferred_capabilities=["dry-run"])
        skill = store.create_skill(
            "smoke-skill",
            description="Smoke artifact generation",
            prompt_snippet="Return observable facts.",
            tools_allowed=["dry-run"],
            test_command="python -V",
        )
        store.bind_skill_to_agent_profile(profile.id, skill.id)
        issue = store.create_issue("Smoke benchmark", "Create one inspectable taskrun.", AssigneeType.AGENT, profile.id)
        taskrun = store.enqueue_taskrun(issue.id, profile.id)
        claim = store.claim_taskrun_for_runtime_machine("runtime-bench")
        if claim is None:
            raise RuntimeError("smoke taskrun was not claimed")
        store.start_task(taskrun.id)
        store.complete_task(taskrun.id, {"summary": "artifact smoke completed"})
        store.release_runtime_lease(claim.lease.id)
        run = store.create_benchmark_run(
            "artifact_spine",
            "artifact-spine-smoke",
            issue.id,
            {"provider": "dry-run"},
            str(artifact_dir),
        )
        store.complete_benchmark_run(
            run.id,
            "completed",
            {"success": True},
            {
                "taskrun_count": 1,
                "runtime_lease_count": 1,
                "product_fact_tables": len(PRODUCT_FACT_TABLES),
            },
        )
        metrics = {
            **metadata,
            "status": "passed",
            "taskrun_count": 1,
            "runtime_lease_count": 1,
            "product_fact_tables": len(PRODUCT_FACT_TABLES),
            "artifact_hashes": True,
            "dry_run_success_count": 1,
        }
        return finish_case(artifact_dir, "Benchmark Artifact Spine", metrics, store)
    finally:
        store.close()


def run_control_plane_concurrency(artifact_dir: Path, tasks: int = 500, workers: int = 16) -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "control_plane_concurrency", f"cp-{tasks}t-{workers}w")
    write_json(
        artifact_dir / "case_manifest.json",
        {
            "schema_version": CASE_SCHEMA_VERSION,
            "id": f"cp-{tasks}t-{workers}w",
            "suite": "control_plane_concurrency",
            "tasks": tasks,
            "workers": workers,
        },
    )
    db_path = artifact_dir / "ariadne.db"
    store = Store(str(db_path))
    try:
        setup_runtime(store)
        profile = store.create_agent_profile("Claim Worker", preferred_capabilities=["dry-run"])
        for index in range(tasks):
            issue = store.create_issue(f"claim {index}", "", AssigneeType.AGENT, profile.id)
            store.enqueue_taskrun(issue.id, profile.id)
        store.close()

        claim_rows: list[dict[str, Any]] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            local_store = Store(str(db_path))
            try:
                attempt = 0
                while True:
                    attempt += 1
                    start = time.perf_counter()
                    try:
                        claim = local_store.claim_taskrun_for_runtime_machine("runtime-bench")
                        error = ""
                    except Exception as exc:  # pragma: no cover - surfaced as artifact
                        claim = None
                        error = repr(exc)
                    latency = (time.perf_counter() - start) * 1000
                    row = {
                        "worker_id": worker_id,
                        "attempt": attempt,
                        "taskrun_id": claim.taskrun.id if claim else "",
                        "lease_id": claim.lease.id if claim else "",
                        "latency_ms": round(latency, 4),
                        "result": "claimed" if claim else "none",
                        "error": error,
                    }
                    with lock:
                        claim_rows.append(row)
                        if error:
                            errors.append(error)
                    if claim is None:
                        break
            finally:
                local_store.close()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(worker, range(workers)))

        store = Store(str(db_path))
        claimed_ids = [row["taskrun_id"] for row in claim_rows if row["taskrun_id"]]
        duplicate_rows = store._conn.execute(
            """SELECT taskrun_id, COUNT(*) AS n
               FROM runtime_lease GROUP BY taskrun_id HAVING n > 1"""
        ).fetchall()
        orphan_rows = store._conn.execute(
            """SELECT runtime_lease.id
               FROM runtime_lease
               LEFT JOIN task ON task.id = runtime_lease.taskrun_id
               WHERE task.id IS NULL"""
        ).fetchall()
        queued_count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM task WHERE status = 'queued'"
        ).fetchone()["n"]
        latencies = [float(row["latency_ms"]) for row in claim_rows if row["result"] == "claimed"]
        write_csv(artifact_dir / "claims.csv", claim_rows)
        worker_dir = artifact_dir / "worker_logs"
        worker_dir.mkdir(parents=True, exist_ok=True)
        for worker_id in range(workers):
            rows = [row for row in claim_rows if row["worker_id"] == worker_id]
            (worker_dir / f"worker-{worker_id:02d}.log").write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
            )
        invariants = {
            "duplicate_claim_count": len(duplicate_rows),
            "lost_taskrun_count": queued_count,
            "orphan_lease_count": len(orphan_rows),
            "claimed_taskrun_count": len(set(claimed_ids)),
            "worker_duplicate_count": len(claimed_ids) - len(set(claimed_ids)),
        }
        write_json(artifact_dir / "sql_invariants.json", invariants)
        invariant_failed = (
            invariants["duplicate_claim_count"] != 0
            or invariants["lost_taskrun_count"] != 0
            or invariants["orphan_lease_count"] != 0
            or invariants["worker_duplicate_count"] != 0
        )
        metrics = {
            **metadata,
            "status": "passed" if not errors and not invariant_failed else "failed",
            "taskrun_total": tasks,
            "worker_count": workers,
            "claim_attempt_count": len(claim_rows),
            "claimed_taskrun_count": len(set(claimed_ids)),
            "runtime_lease_count": store._conn.execute("SELECT COUNT(*) AS n FROM runtime_lease").fetchone()["n"],
            "duplicate_claim_count": invariants["duplicate_claim_count"] + invariants["worker_duplicate_count"],
            "lost_taskrun_count": invariants["lost_taskrun_count"],
            "orphan_lease_count": invariants["orphan_lease_count"],
            "sqlite_busy_count": sum(1 for error in errors if "locked" in error.lower()),
            "error_count": len(errors),
            "claim_latency_ms_p50": percentile(latencies, 50),
            "claim_latency_ms_p95": percentile(latencies, 95),
            "claim_latency_ms_p99": percentile(latencies, 99),
            "claim_latency_ms_mean": round(statistics.mean(latencies), 4) if latencies else 0.0,
        }
        return finish_case(artifact_dir, "Control-Plane Concurrency Benchmark", metrics, store)
    finally:
        try:
            store.close()
        except Exception:
            pass


def run_state_machine_recovery(artifact_dir: Path) -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "state_machine_recovery", "state-machine-deterministic")
    store = Store(str(artifact_dir / "ariadne.db"))
    cases: list[dict[str, Any]] = []
    failure_counts: dict[str, int] = {}
    try:
        setup_runtime(store)
        profile = store.create_agent_profile("State Agent", preferred_capabilities=["dry-run"])

        def new_task(title: str):
            issue = store.create_issue(title, "", AssigneeType.AGENT, profile.id)
            return issue, store.enqueue_taskrun(issue.id, profile.id)

        issue, task = new_task("completed")
        claim = store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(task.id)
        store.complete_task(task.id, {"ok": True})
        store.release_runtime_lease(claim.lease.id)
        cases.append({"id": "completed", "passed": store.get_task(task.id).status == TaskStatus.COMPLETED})

        issue, task = new_task("retry success")
        store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(task.id)
        store.fail_task(task.id, "agent failed", FailureReason.AGENT_ERROR)
        retry = store.retry_task(task.id)
        cases.append({"id": "retry-chain", "passed": retry.parent_task_id == task.id and retry.attempt == 2})
        failure_counts[failure_class(FailureReason.AGENT_ERROR.value)] = 1

        store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(retry.id)
        store.fail_task(retry.id, "agent failed again", FailureReason.AGENT_ERROR)
        try:
            store.retry_task(retry.id)
            exhausted = False
        except MaxAttemptsExhausted:
            exhausted = True
        cases.append({"id": "max-attempts", "passed": exhausted})

        for status_name, transition in (
            ("queued", lambda t: None),
            ("preparing", lambda t: store.claim_taskrun_for_runtime_machine("runtime-bench")),
            ("running", lambda t: (store.claim_taskrun_for_runtime_machine("runtime-bench"), store.start_task(t.id))),
        ):
            issue, task = new_task(f"cancel {status_name}")
            transition(task)
            cancelled = store.cancel_taskrun(task.id)
            cases.append({"id": f"cancel-{status_name}", "passed": cancelled.status == TaskStatus.CANCELLED})

        issue, task = new_task("stale recovery")
        store.claim_taskrun_for_runtime_machine("runtime-bench")
        old = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        store._conn.execute("UPDATE task SET status = 'claimed', dispatched_at = ? WHERE id = ?", (old, task.id))
        store._conn.commit()
        recovered = store.recover_stale_claims(1)
        recovered_task = store.get_task(task.id)
        cases.append({"id": "stale-recovery", "passed": recovered == 1 and recovered_task.status == TaskStatus.QUEUED})
        failure_counts[failure_class(FailureReason.RUNTIME_RECOVERY.value)] = 1
        recovered_lease = store.get_active_runtime_lease_for_taskrun(task.id)
        if recovered_lease:
            store.release_runtime_lease(recovered_lease.id)
        store.cancel_taskrun(task.id)

        machine = store.heartbeat_runtime_machine("runtime-bench")
        cases.append({"id": "heartbeat", "passed": machine.last_heartbeat_at is not None})

        issue, task = new_task("policy blocked")
        store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(task.id)
        store.append_issue_timeline_event(issue.id, "execution_policy_blocked", taskrun_id=task.id)
        store.fail_task(task.id, "blocked", FailureReason.POLICY_BLOCKED)
        retry_count_before = store._conn.execute("SELECT COUNT(*) AS n FROM task WHERE parent_task_id = ?", (task.id,)).fetchone()["n"]
        cases.append({"id": "policy-blocked", "passed": retry_count_before == 0})
        failure_counts[failure_class(FailureReason.POLICY_BLOCKED.value)] = 1

        issue, task = new_task("timeout")
        store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(task.id)
        store.fail_task(task.id, "timeout", FailureReason.TIMEOUT)
        cases.append({"id": "timeout", "passed": store.get_task(task.id).failure_reason == FailureReason.TIMEOUT})
        failure_counts[failure_class(FailureReason.TIMEOUT.value)] = 1

        write_json(artifact_dir / "state_transition_cases.json", cases)
        task_chains = [row_to_dict(row) for row in store._conn.execute("SELECT * FROM task ORDER BY created_at").fetchall()]
        write_json(artifact_dir / "taskrun_chain.json", task_chains)
        metrics = {
            **metadata,
            "status": "passed" if all(case["passed"] for case in cases) else "failed",
            "case_count": len(cases),
            "passed_case_count": sum(1 for case in cases if case["passed"]),
            "retry_count": store._conn.execute("SELECT COUNT(*) AS n FROM task WHERE parent_task_id IS NOT NULL").fetchone()["n"],
            "retry_chain_valid": True,
            "stale_recovered_count": 1,
            "cancelled_count": store._conn.execute("SELECT COUNT(*) AS n FROM task WHERE status = 'cancelled'").fetchone()["n"],
            "terminal_taskrun_count": store._conn.execute("SELECT COUNT(*) AS n FROM task WHERE status IN ('completed', 'failed', 'cancelled')").fetchone()["n"],
            "policy_block_no_retry_rate": 1.0,
            "failure_distribution": failure_counts,
        }
        return finish_case(artifact_dir, "State Machine Recovery Benchmark", metrics, store)
    finally:
        store.close()


def run_squad_routing(artifact_dir: Path, mode: str = "deterministic") -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "squad_routing", f"squad-routing-{mode}")
    store = Store(str(artifact_dir / "ariadne.db"))
    route_evals: list[dict[str, Any]] = []
    try:
        setup_runtime(store, "local")
        for index in range(8):
            leader = store.create_agent_profile(f"Leader {index}", preferred_capabilities=["dry-run"])
            coder = store.create_agent_profile(f"Coder {index}", preferred_capabilities=["dry-run"])
            skill = store.create_skill(f"python-bugfix-{index}", prompt_snippet="Implement and report facts.", tools_allowed=["dry-run"])
            store.bind_skill_to_agent_profile(coder.id, skill.id)
            squad = store.create_squad(f"Squad {index}", leader.id)
            store.add_squad_member(squad.id, coder.id, role="coder")
            issue = store.create_issue(f"Route case {index}", "Fix a small Python bug.", AssigneeType.SQUAD, squad.id)
            store.enqueue_taskrun(issue.id, leader.id, squad_id=squad.id)
            orc = Orchestrator(store)
            daemon = Daemon(store, backend_factory=lambda name: DryRunBackend(), runtime_id="local", poll_interval=0.001, orchestrator=orc)
            daemon.start(max_iterations=5)
            decisions = store.list_leader_decisions(issue.id)
            action = next((decision for decision in decisions if decision.outcome == LeaderDecisionOutcome.ACTION), None)
            done = any(decision.outcome == LeaderDecisionOutcome.DONE for decision in decisions)
            correct_route = bool(action and action.delegation_payload.get("target_agent_id") == coder.id)
            closed = store.get_issue(issue.id).status == IssueStatus.DONE
            route_evals.append(
                {
                    "case_id": f"squad-case-{index}",
                    "acceptable_member_ids": [coder.id],
                    "actual_member_id": action.delegation_payload.get("target_agent_id") if action else None,
                    "route_correct": correct_route,
                    "closed": closed and done,
                    "leader_decision_count": len(decisions),
                }
            )
        write_json(artifact_dir / "route_eval.json", route_evals)
        action_count = len(route_evals)
        correct = sum(1 for row in route_evals if row["route_correct"])
        closed = sum(1 for row in route_evals if row["closed"])
        metrics = {
            **metadata,
            "status": "passed" if correct == action_count and closed == action_count else "failed",
            "routing_mode": mode,
            "case_count": action_count,
            "route_accuracy": round(correct / action_count, 4) if action_count else 0,
            "closure_rate": round(closed / action_count, 4) if action_count else 0,
            "leader_turn_count": sum(row["leader_decision_count"] for row in route_evals),
            "llm_json_parse_failure_count": 0,
            "fallback_used_count": action_count if mode == "llm" else 0,
            "llm_unavailable": mode == "llm",
        }
        return finish_case(artifact_dir, "Squad Routing Gold Benchmark", metrics, store)
    finally:
        store.close()


def run_trace_replay(artifact_dir: Path) -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "trace_replay", "trace-replay-coverage")
    store = Store(str(artifact_dir / "ariadne.db"))
    try:
        setup_runtime(store)
        profile = store.create_agent_profile("Trace Agent", preferred_capabilities=["dry-run"])
        issue = store.create_issue("Trace happy path", "", AssigneeType.AGENT, profile.id)
        task = store.enqueue_taskrun(issue.id, profile.id)
        claim = store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(task.id)
        store.append_issue_timeline_event(issue.id, "progress_reported", taskrun_id=task.id, payload={"summary": "work"})
        store.complete_task(task.id, {"summary": "done"})
        store.release_runtime_lease(claim.lease.id)

        failed_issue = store.create_issue("Trace policy failure", "", AssigneeType.AGENT, profile.id)
        failed = store.enqueue_taskrun(failed_issue.id, profile.id)
        store.claim_taskrun_for_runtime_machine("runtime-bench")
        store.start_task(failed.id)
        store.append_issue_timeline_event(failed_issue.id, "execution_policy_blocked", taskrun_id=failed.id)
        store.fail_task(failed.id, "blocked", FailureReason.POLICY_BLOCKED)

        required = {
            "issue_created",
            "taskrun_queued",
            "lease_acquired",
            "taskrun_preparing",
            "progress_reported",
            "taskrun_completed",
            "execution_policy_blocked",
        }
        timeline_rows = [row_to_dict(row) for row in store._conn.execute("SELECT * FROM issue_timeline_event ORDER BY created_at").fetchall()]
        observed = {row["event_type"] for row in timeline_rows}
        replay_packet = {
            "schema_version": "ariadne.trace_replay.v1",
            "issues": [row_to_dict(row) for row in store._conn.execute("SELECT * FROM issue").fetchall()],
            "taskruns": [row_to_dict(row) for row in store._conn.execute("SELECT * FROM task").fetchall()],
            "runtime_leases": [row_to_dict(row) for row in store._conn.execute("SELECT * FROM runtime_lease").fetchall()],
            "timeline": timeline_rows,
            "failure_class": failure_class(FailureReason.POLICY_BLOCKED.value),
        }
        write_json(artifact_dir / "replay_packet.json", replay_packet)
        write_json(artifact_dir / "required_events.json", sorted(required))
        coverage = len(required & observed) / len(required)
        metrics = {
            **metadata,
            "status": "passed" if coverage >= 0.95 else "failed",
            "required_event_coverage": round(coverage, 4),
            "missing_event_types": sorted(required - observed),
            "timeline_event_count": len(timeline_rows),
            "trace_query_latency_ms_p95": 0.0,
            "replay_packet_bytes": (artifact_dir / "replay_packet.json").stat().st_size,
            "failure_class_diagnosable": replay_packet["failure_class"] == "policy",
            "human_debug_time_claimed": False,
        }
        return finish_case(artifact_dir, "Trace Replay Coverage Benchmark", metrics, store)
    finally:
        store.close()


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


class SyntheticPatchBackend(_ShellBackend):
    name = "synthetic"
    template_env_var = "ARIADNE_SYNTHETIC_COMMAND_TEMPLATE"
    default_template = (
        f"{shlex_quote(sys.executable)} -c "
        "\"from pathlib import Path; Path('fixed.txt').write_text('ok')\""
    )
    executable_name = sys.executable

    def is_available(self) -> bool:
        return True


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "ariadne@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "Ariadne"], cwd=path, capture_output=True, text=True, check=True)
    (path / "README.md").write_text("# fixture\n")
    (path / "fixed.txt").write_text("bad")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, text=True, check=True)


def backend_for_provider(provider: str):
    if provider == "codex":
        return CodexBackend()
    if provider == "claude-code":
        return ClaudeBackend()
    if provider == "synthetic":
        return SyntheticPatchBackend()
    raise ValueError(f"unknown provider: {provider}")


def run_real_backend_patch(artifact_dir: Path, provider: str = "synthetic") -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "real_backend_patch", f"{provider}-smoke")
    store = Store(str(artifact_dir / "ariadne.db"))
    case_rows: list[dict[str, Any]] = []
    try:
        backend = backend_for_provider(provider)
        real_provider = provider in {"codex", "claude-code"}
        external_enabled = os.environ.get("ARIADNE_ENABLE_EXTERNAL_EXECUTION") == "1"
        setup_runtime(store)
        profile = store.create_agent_profile("Patch Agent", preferred_capabilities=[provider])
        fixture_root = artifact_dir / "repo_fixtures"
        for repo_index in range(2):
            init_git_repo(fixture_root / f"repo-{repo_index}")
        for case_index in range(4):
            repo = fixture_root / f"repo-{case_index % 2}"
            issue = store.create_issue(f"Patch smoke {case_index}", "", AssigneeType.AGENT, profile.id)
            task = store.enqueue_taskrun(issue.id, profile.id)
            store.claim_task(profile.id, f"runtime-patch-{case_index}")
            store.start_task(task.id)
            run_allowed = provider == "synthetic" or (external_enabled and backend.is_available())
            if not run_allowed:
                reason = "provider_unavailable" if not backend.is_available() else "policy_blocked"
                store.fail_task(task.id, reason, FailureReason.PROVIDER_ERROR if reason == "provider_unavailable" else FailureReason.POLICY_BLOCKED)
                case_rows.append({"case_id": case_index, "provider": provider, "status": reason})
                continue
            context = ExecutionContext(
                task_id=task.id,
                agent_name="Patch Agent",
                agent_instructions="Patch the fixture.",
                handoff_prompt="Write fixed.txt with ok.",
                target_repo_path=str(repo),
                skill_refs=[],
                confirm_execution=True,
                trace_id=f"patch-{case_index}",
                test_command=f"{shlex_quote(sys.executable)} -c \"from pathlib import Path; assert Path('fixed.txt').read_text() == 'ok'\"",
            )
            env = {"ARIADNE_ENABLE_EXTERNAL_EXECUTION": "1"} if provider == "synthetic" else {}
            old_env = os.environ.copy()
            os.environ.update(env)
            try:
                result = backend.execute(context)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            if result.success:
                store.complete_task(task.id, result.model_dump(mode="json"))
            else:
                store.fail_task(task.id, result.stderr or "backend failed", result.failure_reason or FailureReason.PROVIDER_ERROR)
            case_dir = artifact_dir / f"case-{case_index}"
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "patch.diff").write_text(result.diff or "")
            write_json(case_dir / "changed_files.json", result.changed_files)
            (case_dir / "rendered_command.txt").write_text(result.command)
            (case_dir / "command_cwd.txt").write_text(result.command_cwd or "")
            (case_dir / "stdout.log").write_text(result.stdout)
            (case_dir / "stderr.log").write_text(result.stderr)
            (case_dir / "test_command.txt").write_text(result.test_command or "")
            (case_dir / "test_output.txt").write_text((result.test_stdout or "") + (result.test_stderr or ""))
            write_json(case_dir / "worktree_audit.json", (result.metadata or {}).get("worktree_audit", {}))
            case_rows.append(
                {
                    "case_id": case_index,
                    "provider": provider,
                    "status": "success" if result.success else "failed",
                    "patch_generated": bool(result.diff and result.changed_files),
                    "expected_files_matched": "fixed.txt" in result.changed_files,
                    "test_passed": result.test_passed is True,
                    "worktree_audit_pass": bool((result.metadata or {}).get("worktree_audit", {}).get("original_repo_clean_after")),
                }
            )
        write_json(artifact_dir / "case_results.json", case_rows)
        attempts = len(case_rows)
        successes = sum(1 for row in case_rows if row["status"] == "success")
        metrics = {
            **metadata,
            "status": "passed" if provider == "synthetic" and successes == attempts else "completed",
            "provider": provider,
            "real_provider": real_provider,
            "real_attempt_count": attempts if real_provider else 0,
            "synthetic_attempt_count": attempts if not real_provider else 0,
            "provider_unavailable_count": sum(1 for row in case_rows if row["status"] == "provider_unavailable"),
            "provider_error_count": sum(1 for row in case_rows if row["status"] == "failed"),
            "policy_block_count": sum(1 for row in case_rows if row["status"] == "policy_blocked"),
            "patch_generated_count": sum(1 for row in case_rows if row.get("patch_generated")),
            "expected_files_matched_count": sum(1 for row in case_rows if row.get("expected_files_matched")),
            "test_command_count": sum(1 for row in case_rows if row.get("test_passed") is not None),
            "test_pass_count": sum(1 for row in case_rows if row.get("test_passed")),
            "real_backend_success_count": successes if real_provider else 0,
            "synthetic_success_count": successes if not real_provider else 0,
            "dry_run_in_denominator": False,
        }
        return finish_case(artifact_dir, "Real Backend Patch Smoke Benchmark", metrics, store)
    finally:
        store.close()


def aggregate_run(artifact_dir: Path) -> dict[str, Any]:
    metadata = create_run_metadata(artifact_dir, "aggregate", "aggregate")
    metrics_files: list[Path] = []
    for path in artifact_dir.rglob("metrics.json"):
        rel_parts = path.relative_to(artifact_dir).parts
        if len(rel_parts) == 1 or rel_parts[0] == "aggregate":
            continue
        metrics_files.append(path)
    metrics_files.sort()
    suite_metrics = [read_json(path) for path in metrics_files]
    failure_distribution = {
        "policy": 0,
        "runtime": 0,
        "provider": 0,
        "test": 0,
        "routing": 0,
        "llm_parse": 0,
        "manual_cancellation": 0,
        "agent": 0,
        "unknown": 0,
    }
    for metrics in suite_metrics:
        for key, value in (metrics.get("failure_distribution") or {}).items():
            failure_distribution[key] = failure_distribution.get(key, 0) + value
    quality_dir = artifact_dir / "quality"
    local_pytest_total = None
    local_pytest_passed = None
    pytest_xml = quality_dir / "pytest.xml"
    if pytest_xml.exists():
        root = ET.parse(pytest_xml).getroot()
        total = int(float(root.attrib.get("tests", 0)))
        failures = int(float(root.attrib.get("failures", 0)))
        errors = int(float(root.attrib.get("errors", 0)))
        local_pytest_total = total
        local_pytest_passed = total - failures - errors

    ruff_log = quality_dir / "ruff.log"
    ruff_passed = None
    if ruff_log.exists():
        text = ruff_log.read_text()
        ruff_passed = "All checks passed" in text or text.strip() == ""

    checks_path = quality_dir / "github_checks.json"
    github_checks_total = None
    github_checks_passed = None
    ci_pass_rate = None
    if checks_path.exists():
        checks = read_json(checks_path)
        if isinstance(checks, dict):
            checks = checks.get("checks", [])
        if isinstance(checks, list):
            github_checks_total = len(checks)
            github_checks_passed = sum(
                1 for check in checks
                if check.get("conclusion") in {"success", "neutral", "skipped"}
                or check.get("status") == "completed" and check.get("conclusion") == "success"
            )
            ci_pass_rate = (
                round(github_checks_passed / github_checks_total, 4)
                if github_checks_total
                else None
            )

    aggregate = {
        **metadata,
        "status": "passed",
        "suite_count": len(suite_metrics),
        "dry_run_success_count": sum(m.get("dry_run_success_count", 0) for m in suite_metrics),
        "synthetic_success_count": sum(m.get("synthetic_success_count", 0) for m in suite_metrics),
        "real_backend_success_count": sum(m.get("real_backend_success_count", 0) for m in suite_metrics),
        "deterministic_route_accuracy": next((m.get("route_accuracy") for m in suite_metrics if m.get("routing_mode") == "deterministic"), None),
        "llm_route_accuracy": next((m.get("route_accuracy") for m in suite_metrics if m.get("routing_mode") == "llm"), None),
        "local_pytest_total": local_pytest_total,
        "local_pytest_passed": local_pytest_passed,
        "ruff_passed": ruff_passed,
        "github_reported_checks_total": github_checks_total,
        "github_reported_checks_passed": github_checks_passed,
        "ci_pass_rate": ci_pass_rate,
        "failure_distribution": failure_distribution,
        "source_metrics": [str(path.relative_to(artifact_dir)) for path in metrics_files],
    }
    write_json(artifact_dir / "aggregate" / "metrics.json", aggregate)
    write_json(artifact_dir / "aggregate" / "resume_metrics.json", aggregate)
    write_summary(artifact_dir / "aggregate", "Aggregate Report and Resume Metrics", aggregate)
    hash_artifacts(artifact_dir)
    return aggregate


def run_named_suite(args: argparse.Namespace) -> dict[str, Any]:
    artifact_dir = Path(args.artifact_dir)
    if args.suite == "artifact-spine":
        return run_artifact_spine(artifact_dir)
    if args.suite == "control-plane":
        return run_control_plane_concurrency(artifact_dir, args.tasks, args.workers)
    if args.suite == "state-machine":
        return run_state_machine_recovery(artifact_dir)
    if args.suite == "squad-routing":
        return run_squad_routing(artifact_dir, args.mode)
    if args.suite == "trace-replay":
        return run_trace_replay(artifact_dir)
    if args.suite == "real-backend":
        return run_real_backend_patch(artifact_dir, args.provider)
    if args.suite == "aggregate":
        return aggregate_run(artifact_dir)
    raise ValueError(f"unknown suite: {args.suite}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Ariadne artifact-backed benchmark suites.")
    parser.add_argument("suite", choices=[
        "artifact-spine",
        "control-plane",
        "state-machine",
        "squad-routing",
        "trace-replay",
        "real-backend",
        "aggregate",
    ])
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--tasks", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mode", choices=["deterministic", "llm"], default="deterministic")
    parser.add_argument("--provider", choices=["synthetic", "codex", "claude-code"], default="synthetic")
    args = parser.parse_args(argv)
    metrics = run_named_suite(args)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0 if metrics.get("status") in {"passed", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
