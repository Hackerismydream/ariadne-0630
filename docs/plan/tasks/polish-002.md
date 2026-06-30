id: polish-002
scope: polish
status: pending
depends-on: [polish-001]
```

## Objective

Fill test coverage gaps, collect benchmark data, and produce the final
metrics that fill the resume `{待测}` placeholders. This is the closing task.

## Context

- Resume template: multica-python-调查报告.md
- Benchmark: src/ariadne/eval.py (from eval-001)

## Path

```
tests/test_coverage_gaps.py   # new — cover any untested paths
scripts/run_benchmark.sh      # new — runs benchmark and saves report
benchmark_report.json         # generated — the actual numbers
```

## Requirements

### Coverage gaps to fill

Review test coverage and add tests for:
- `cancel_task` from queued/claimed/running states
- `get_squad_leader` with missing squad
- `briefing.generate_briefing` with missing squad
- `orchestrator` with task that has no squad_id (should error gracefully)
- `daemon` with no agents (poll_once returns None)

### Benchmark script

```bash
#!/bin/bash
# Run benchmark with dry-run backend and save report
uv run ariadne benchmark run --backend dry-run --iterations 10 > benchmark_report.json
```

### Benchmark metrics to collect

Run 10 benchmark tasks (dry-run) and record:
- total tasks
- success rate
- avg duration
- retry count
- failure reason breakdown

These numbers fill the resume placeholders.

### Constraints

- All existing tests must still pass
- Benchmark uses dry-run only (no real CLI calls)
- Script is executable and idempotent

## Verification

```bash
ruff check tests/test_coverage_gaps.py
pytest tests/ -v  # all tests pass
bash scripts/run_benchmark.sh  # produces benchmark_report.json
```
