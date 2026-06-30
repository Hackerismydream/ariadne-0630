"""LLM-as-judge evaluation + benchmark harness.

Scores completed task results and collects metrics for resume placeholders.
LLM evaluation is optional — falls back to deterministic scoring without a key.

Per docs/plan/tasks/eval-001.md.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ariadne.models import TaskStatus
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


@dataclass
class BenchmarkReport:
    total_tasks: int
    success_count: int
    failure_count: int
    avg_score: float
    avg_duration_seconds: float
    retry_count: int
    failure_reasons: dict[str, int] = field(default_factory=dict)
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
    """Default execution: create agent + issue + enqueue + simulate."""
    from ariadne.models import AssigneeType

    agent = store.create_agent(f"Benchmark-{bt.title}", "", [bt.backend], [])
    issue = store.create_issue(bt.title, bt.description, AssigneeType.AGENT, agent.id)
    task = store.enqueue_task(issue.id, agent.id)

    # Simulate execution via daemon with dry-run
    from ariadne.backends import get_backend
    from ariadne.daemon import Daemon

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        poll_interval=0.001,
    )
    daemon.start(max_iterations=3)

    finished = store.get_task(task.id)
    success = finished.status == TaskStatus.COMPLETED if finished else False
    duration = 0.0
    if finished and finished.completed_at and finished.started_at:
        duration = (finished.completed_at - finished.started_at).total_seconds()

    return {
        "task_id": task.id,
        "title": bt.title,
        "success": success,
        "duration_seconds": duration,
        "retry_count": 0,
        "failure_reason": finished.failure_reason.value if finished and finished.failure_reason else None,
    }


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
    for r in results:
        reason = r.get("failure_reason")
        if reason:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    return BenchmarkReport(
        total_tasks=total,
        success_count=success_count,
        failure_count=failure_count,
        avg_score=round(avg_score, 2),
        avg_duration_seconds=round(avg_duration, 4),
        retry_count=retry_count,
        failure_reasons=failure_reasons,
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
        "tasks": report.tasks,
    }
