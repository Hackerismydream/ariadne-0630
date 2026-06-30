id: eval-001
scope: eval
status: done
depends-on: [backend-002]
```

## Objective

Implement a lightweight LLM-as-judge evaluation layer that scores completed
task results, plus a benchmark harness that runs a fixed set of tasks and
collects metrics for the resume `{待测}` placeholders.

## Context

- Doc index: [docs/INDEX.md](../../INDEX.md)
- Resume template: multica-python-调查报告.md (the {待测} placeholders)

## Path

```
src/ariadne/eval.py
tests/test_eval.py
```

## Requirements

### eval.py

```python
def evaluate_task(store: Store, task_id: str, api_key: str | None = None) -> EvaluationResult:
    """Score a completed task's result using LLM-as-judge.

    If no API key: return a deterministic score based on task success/failure.
    If API key: call LLM with the task result + issue, ask for a 1-5 score
    on correctness and completeness.
    """

def run_benchmark(store: Store, tasks: list[BenchmarkTask]) -> BenchmarkReport:
    """Run a set of benchmark tasks and collect metrics.

    For each task: enqueue → daemon execute → evaluate → record metrics.
    Returns a report with: task count, success rate, avg score, avg duration,
    retry count, failure reasons breakdown.
    """

@dataclass
class EvaluationResult:
    task_id: str
    score: int          # 1-5
    reasoning: str
    evaluated_at: str   # ISO timestamp

@dataclass
class BenchmarkTask:
    title: str
    description: str
    backend: str        # "dry-run" | "codex" | "claude-code"
    expected_success: bool

@dataclass
class BenchmarkReport:
    total_tasks: int
    success_count: int
    failure_count: int
    avg_score: float
    avg_duration_seconds: float
    retry_count: int
    failure_reasons: dict[str, int]  # reason → count
    tasks: list[dict]   # per-task detail
```

### Deterministic fallback (no API key)

When no LLM key is available:
- task completed → score 5
- task failed → score 1
- task cancelled → score 0

### LLM-as-judge prompt

```
You are evaluating an AI agent's task execution. Score 1-5.

Issue: {issue.title} - {issue.description}
Result: {task.result}
Success: {task.success}

1 = completely wrong, 2 = mostly wrong, 3 = partially correct,
4 = mostly correct, 5 = fully correct.

Respond with JSON: {"score": N, "reasoning": "..."}
```

### Benchmark metrics (fills resume {待测} placeholders)

The benchmark report provides data for:
- `{待测}` success rate per backend
- `{待测}` avg duration
- `{待测}` retry count
- `{待测}` failure reason breakdown

### CLI command

```
ariadne benchmark run --backend dry-run --iterations 5
```

Runs N benchmark tasks and prints the report.

### Constraints

- eval.py imports: models, store, json, logging, os, dataclasses, datetime
- LLM call is optional (graceful fallback)
- No new dependencies
- Benchmark uses DryRunBackend by default (real backends gated)

## Verification

```bash
ruff check src/ariadne/eval.py
pytest tests/test_eval.py -v
```

### test_eval.py must cover:

- `test_evaluate_completed_task`: completed task → score 5 (deterministic)
- `test_evaluate_failed_task`: failed task → score 1 (deterministic)
- `test_evaluate_cancelled_task`: cancelled task → score 0 (deterministic)
- `test_evaluate_missing_task`: task not found → raises KeyError
- `test_benchmark_run`: 3 tasks → report with correct totals
- `test_benchmark_report_has_metrics`: report has success_count, avg_score, avg_duration, retry_count
- `test_benchmark_failure_breakdown`: mix of success/failure → failure_reasons dict populated
