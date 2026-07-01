"""LLM-as-judge evaluation + benchmark harness.

Scores completed task results and collects metrics for resume placeholders.
LLM evaluation is optional — falls back to deterministic scoring without a key.

Per docs/plan/tasks/eval-001.md.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ariadne.models import ExecutionContext, FailureReason, IssueStatus, TaskStatus
from ariadne.store import Store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvaluationResult:
    task_id: str
    score: int  # 0-5
    reasoning: str
    evaluated_at: str  # ISO timestamp


@dataclass
class BenchmarkTask:
    title: str
    description: str
    backend: str
    expected_success: bool
    suite_name: str = "default"
    runtime_policy: dict = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    total_tasks: int
    success_count: int
    failure_count: int
    avg_score: float
    avg_duration_seconds: float
    retry_count: int
    failure_reasons: dict[str, int] = field(default_factory=dict)
    failure_classes: dict[str, int] = field(default_factory=dict)
    benchmark_run_ids: list[str] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_task(
    store: Store, task_id: str, api_key: str | None = None
) -> EvaluationResult:
    """Score a completed task's result.

    If no API key: deterministic score based on task status.
    If API key: LLM-as-judge with 1-5 score.
    """
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(f"task not found: {task_id}")

    key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return _deterministic_score(task)

    return _llm_score(store, task, key)


def _deterministic_score(task) -> EvaluationResult:
    """Score based on task status — no LLM needed."""
    now = datetime.now(timezone.utc).isoformat()
    if task.status == TaskStatus.COMPLETED:
        return EvaluationResult(task_id=task.id, score=5, reasoning="task completed successfully", evaluated_at=now)
    if task.status == TaskStatus.FAILED:
        return EvaluationResult(task_id=task.id, score=1, reasoning="task failed", evaluated_at=now)
    return EvaluationResult(task_id=task.id, score=0, reasoning=f"task status: {task.status.value}", evaluated_at=now)


def _llm_score(store: Store, task, api_key: str) -> EvaluationResult:
    """Call LLM to evaluate task result. Falls back to deterministic on error."""
    now = datetime.now(timezone.utc).isoformat()
    issue = store.get_issue(task.issue_id)
    issue_text = f"{issue.title} - {issue.description}" if issue else "unknown issue"
    result_text = json.dumps(task.result) if task.result else "no result"
    success_text = "true" if task.status == TaskStatus.COMPLETED else "false"

    prompt = f"""You are evaluating an AI agent's task execution. Score 1-5.

Issue: {issue_text}
Result: {result_text}
Success: {success_text}

1 = completely wrong, 2 = mostly wrong, 3 = partially correct,
4 = mostly correct, 5 = fully correct.

Respond with JSON only: {{"score": N, "reasoning": "..."}}
"""

    try:
        from openai import OpenAI

        base_url = os.environ.get("ARIADNE_LLM_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("ARIADNE_LLM_MODEL", "deepseek-chat")
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a task evaluator. Respond with JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        text = response.choices[0].message.content or "{}"
        data = json.loads(text.strip())
        return EvaluationResult(
            task_id=task.id,
            score=int(data.get("score", 3)),
            reasoning=data.get("reasoning", "LLM evaluation"),
            evaluated_at=now,
        )
    except Exception as e:
        logger.error("LLM evaluation failed, using deterministic: %s", e)
        return _deterministic_score(task)


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def run_benchmark(
    store: Store,
    tasks: list[BenchmarkTask],
    execute_fn=None,
) -> BenchmarkReport:
    """Run benchmark tasks and collect metrics.

    execute_fn: callable(task: BenchmarkTask) -> dict with keys:
        task_id, success, duration_seconds, retry_count, failure_reason
    If None, uses a default that enqueues + executes via dry-run.
    """
    results: list[dict] = []

    for bt in tasks:
        if execute_fn:
            result = execute_fn(bt)
        else:
            result = _default_execute(store, bt)
        results.append(result)

    return _build_report(results)


def _default_execute(store: Store, bt: BenchmarkTask) -> dict:
    """Default execution: seed product objects and compute facts after daemon run."""
    from ariadne.backends import get_backend
    from ariadne.daemon import Daemon
    from ariadne.models import (
        AssigneeType,
        DelegationDecision,
        LeaderDecision,
        LeaderDecisionOutcome,
    )
    from ariadne.orchestrator import Orchestrator

    slug = _slug(bt.title)
    artifact_dir = tempfile.mkdtemp(prefix=f"ariadne-benchmark-{slug}-")
    runtime_policy = {
        "backend": bt.backend,
        "expected_success": bt.expected_success,
        **bt.runtime_policy,
    }

    leader = store.create_agent_profile(
        name=f"Benchmark Leader {bt.title}",
        instructions="Coordinate the benchmark case.",
        preferred_capabilities=["dry-run"],
        runtime_policy={"allow_real_execution": False},
    )
    member = store.create_agent_profile(
        name=f"Benchmark Worker {bt.title}",
        instructions="Execute the benchmark case.",
        preferred_capabilities=[bt.backend],
        runtime_policy=bt.runtime_policy,
    )
    skill = store.create_skill(
        name=f"benchmark-{slug}-{len(store.list_skills()) + 1}",
        description="Benchmark case skill",
        when_to_use=bt.description,
        prompt_snippet="Execute the benchmark case and report observable facts.",
        tools_allowed=[bt.backend],
        test_command=bt.runtime_policy.get("test_command"),
        source_path="benchmark",
        version="1",
    )
    store.bind_skill_to_agent_profile(member.id, skill.id)

    squad = store.create_squad(
        f"Benchmark Squad {bt.title}",
        leader.id,
        instructions="Delegate once, then evaluate facts and close when done.",
    )
    store.add_squad_member(squad.id, member.id, role="benchmark-worker")
    issue = store.create_issue(bt.title, bt.description, AssigneeType.SQUAD, squad.id)
    run = store.create_benchmark_run(
        suite_name=bt.suite_name,
        case_name=bt.title,
        issue_id=issue.id,
        runtime_policy=runtime_policy,
        artifact_dir=artifact_dir,
    )
    store.enqueue_taskrun(issue.id, leader.id, squad_id=squad.id)

    call_count = [0]

    def decide(briefing, issue, completed_results=None):
        call_count[0] += 1
        if not completed_results:
            entry = briefing.roster[0]
            return DelegationDecision(
                target_agent_id=entry.agent_id,
                backend=bt.backend,
                handoff_prompt=f"{bt.title}\n\n{bt.description}",
                reason="benchmark action",
                skill_refs=entry.skills,
            )
        return LeaderDecision(
            outcome=LeaderDecisionOutcome.DONE,
            reason="benchmark member task produced terminal facts",
        )

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        poll_interval=0.001,
        orchestrator=Orchestrator(store=store, llm_decide=decide),
        target_repo_path=artifact_dir,
    )
    daemon.start(max_iterations=10)

    refreshed_issue = store.get_issue(issue.id)
    metrics = collect_benchmark_metrics(store, issue.id)
    success = (
        refreshed_issue is not None
        and refreshed_issue.status == IssueStatus.DONE
        and metrics["failed_taskrun_count"] == 0
    )
    passed = success == bt.expected_success
    summary = {
        "success": success,
        "expected_success": bt.expected_success,
        "passed": passed,
        "failure_classes": metrics["failure_classes"],
    }
    run = store.complete_benchmark_run(
        run.id,
        status="completed" if passed else "failed",
        summary=summary,
        metrics=metrics,
    )

    return {
        "benchmark_run_id": run.id,
        "issue_id": issue.id,
        "task_id": metrics["primary_taskrun_id"],
        "title": bt.title,
        "success": success,
        "duration_seconds": metrics["duration_seconds"],
        "retry_count": metrics["retry_count"],
        "failure_reason": metrics["primary_failure_reason"],
        "failure_classes": metrics["failure_classes"],
        "metrics": metrics,
    }


def collect_benchmark_metrics(store: Store, issue_id: str) -> dict:
    """Compute BenchmarkRun metrics strictly from persisted product facts."""
    rows = store._conn.execute(
        "SELECT * FROM task WHERE issue_id = ? ORDER BY created_at", (issue_id,)
    ).fetchall()
    taskruns = [store._row_to_taskrun(row) for row in rows]
    timeline = store.get_issue_timeline(issue_id)
    leader_decisions = store.list_leader_decisions(issue_id)
    leases = []
    for taskrun in taskruns:
        leases.extend(store.list_runtime_leases(taskrun.id))

    failed = [taskrun for taskrun in taskruns if taskrun.status == TaskStatus.FAILED]
    completed = [
        taskrun for taskrun in taskruns if taskrun.status == TaskStatus.COMPLETED
    ]
    retry_count = sum(1 for taskrun in taskruns if taskrun.parent_taskrun_id)
    failure_reasons: dict[str, int] = {}
    failure_classes: dict[str, int] = {}
    primary_failure_reason = None
    for taskrun in failed:
        if taskrun.failure_reason is None:
            continue
        reason = taskrun.failure_reason.value
        primary_failure_reason = primary_failure_reason or reason
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        failure_class = classify_failure(reason, timeline)
        failure_classes[failure_class] = failure_classes.get(failure_class, 0) + 1

    durations = []
    for taskrun in taskruns:
        if taskrun.started_at and taskrun.completed_at:
            durations.append((taskrun.completed_at - taskrun.started_at).total_seconds())

    policy_blocks = [
        event for event in timeline if event.event_type == "execution_policy_blocked"
    ]
    lease_events = [
        event for event in timeline if event.event_type in {"lease_expired", "lease_revoked"}
    ]
    return {
        "taskrun_count": len(taskruns),
        "completed_taskrun_count": len(completed),
        "failed_taskrun_count": len(failed),
        "runtime_lease_count": len(leases),
        "leader_decision_count": len(leader_decisions),
        "issue_timeline_event_count": len(timeline),
        "policy_block_count": len(policy_blocks),
        "lease_event_count": len(lease_events),
        "retry_count": retry_count,
        "duration_seconds": round(sum(durations), 4),
        "failure_reasons": failure_reasons,
        "failure_classes": failure_classes,
        "primary_failure_reason": primary_failure_reason,
        "primary_taskrun_id": taskruns[0].id if taskruns else None,
    }


def classify_failure(reason: str, timeline: list | None = None) -> str:
    """Map failure reasons and timeline facts into report buckets."""
    timeline = timeline or []
    if any(event.event_type == "execution_policy_blocked" for event in timeline):
        return "policy"
    if any(event.event_type == "lease_expired" for event in timeline):
        return "lease"
    if reason == FailureReason.POLICY_BLOCKED.value:
        return "policy"
    if reason in {
        FailureReason.RUNTIME_OFFLINE.value,
        FailureReason.RUNTIME_RECOVERY.value,
        FailureReason.TIMEOUT.value,
    }:
        return "runtime"
    if reason == FailureReason.MANUAL.value:
        return "manual_cancellation"
    if reason == "provider_error":
        return "provider"
    if reason == "verifier_error":
        return "verifier"
    if reason == "test_failure":
        return "test"
    return "agent"


def _build_report(results: list[dict]) -> BenchmarkReport:
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    failure_count = total - success_count

    scores = []
    for r in results:
        if r["success"]:
            scores.append(5)
        elif r.get("failure_reason"):
            scores.append(1)
        else:
            scores.append(0)

    avg_score = sum(scores) / len(scores) if scores else 0.0
    durations = [r["duration_seconds"] for r in results]
    avg_duration = sum(durations) / len(durations) if durations else 0.0
    retry_count = sum(r.get("retry_count", 0) for r in results)

    failure_reasons: dict[str, int] = {}
    failure_classes: dict[str, int] = {}
    benchmark_run_ids: list[str] = []
    for r in results:
        if r.get("benchmark_run_id"):
            benchmark_run_ids.append(r["benchmark_run_id"])
        reason = r.get("failure_reason")
        if reason:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        result_failure_classes = r.get("failure_classes", {})
        if result_failure_classes:
            classes_to_merge = result_failure_classes
        elif reason:
            classes_to_merge = {classify_failure(reason): 1}
        else:
            classes_to_merge = {}
        for failure_class, count in classes_to_merge.items():
            failure_classes[failure_class] = failure_classes.get(failure_class, 0) + count

    return BenchmarkReport(
        total_tasks=total,
        success_count=success_count,
        failure_count=failure_count,
        avg_score=round(avg_score, 2),
        avg_duration_seconds=round(avg_duration, 4),
        retry_count=retry_count,
        failure_reasons=failure_reasons,
        failure_classes=failure_classes,
        benchmark_run_ids=benchmark_run_ids,
        tasks=results,
    )


def report_to_dict(report: BenchmarkReport) -> dict:
    """Serialize report to dict for JSON output."""
    return {
        "total_tasks": report.total_tasks,
        "success_count": report.success_count,
        "failure_count": report.failure_count,
        "success_rate": round(report.success_count / report.total_tasks * 100, 1) if report.total_tasks else 0,
        "avg_score": report.avg_score,
        "avg_duration_seconds": report.avg_duration_seconds,
        "retry_count": report.retry_count,
        "failure_reasons": report.failure_reasons,
        "failure_classes": report.failure_classes,
        "benchmark_run_ids": report.benchmark_run_ids,
        "tasks": report.tasks,
    }


def run_single_vs_squad(
    store: Store,
    num_member_tasks: int = 3,
    task_duration: float = 0.1,
    backend: str = "dry-run",
    max_concurrent: int | None = None,
) -> dict:
    """Compare serial and bounded-parallel execution with explicit evidence labels.

    Dry-run mode injects deterministic latency and is reported as simulated.
    Real backend mode uses the requested backend against a temporary workspace;
    backend isolation policy decides whether the subprocess may run.
    """
    from ariadne.backends import get_backend

    del store
    task_count = max(1, num_member_tasks)
    concurrency = max(1, min(max_concurrent or min(task_count, os.cpu_count() or 4), task_count))
    simulated = backend == "dry-run"

    backend_impl = get_backend(backend)
    target_repo = tempfile.mkdtemp(prefix="ariadne-compare-")
    try:
        def execute_one(index: int):
            if simulated:
                time.sleep(task_duration)
            context = ExecutionContext(
                task_id=f"compare-{backend}-{index}",
                agent_name="BenchmarkAgent",
                agent_instructions="Run the comparison task and report facts.",
                handoff_prompt=f"Comparison sub-task {index}",
                target_repo_path=target_repo,
                skill_refs=[],
                confirm_execution=True,
                trace_id=f"compare-{index}",
            )
            return backend_impl.execute(context)

        single_started = time.monotonic()
        single_results = [execute_one(index) for index in range(task_count)]
        single_duration = time.monotonic() - single_started

        squad_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            squad_results = list(executor.map(execute_one, range(task_count)))
        squad_duration = time.monotonic() - squad_started

        single_success = all(result.success for result in single_results)
        squad_success = all(result.success for result in squad_results)
        speedup = single_duration / squad_duration if squad_duration > 0 else 0.0
        return {
            "backend": backend,
            "max_concurrent": concurrency,
            "simulated": simulated,
            "status": "completed" if single_success and squad_success else "failed",
            "single": {
                "mode": "single",
                "total_duration": round(single_duration, 4),
                "task_count": task_count,
                "parallelism": 1,
                "success": single_success,
            },
            "squad": {
                "mode": "squad",
                "total_duration": round(squad_duration, 4),
                "task_count": task_count,
                "parallelism": concurrency,
                "success": squad_success,
            },
            "speedup": round(speedup, 2),
        }
    finally:
        shutil.rmtree(target_repo, ignore_errors=True)


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "case"
