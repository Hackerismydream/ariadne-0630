id: deep-001
scope: trace
status: pending
depends-on: []
```

## Objective

Add trace_id to Task model, activity_log table, log propagation via LoggerAdapter, and `ariadne task-timeline` CLI command.

## Context

- Design doc: [docs/architecture/trace-observability.md](../../architecture/trace-observability.md)

## Path

```
src/ariadne/models.py        # add trace_id field to Task
src/ariadne/store.py         # generate trace_id, activity_log table, log_activity(), retry inherits trace_id
src/ariadne/orchestrator.py  # child tasks inherit leader trace_id, log_activity at delegation
src/ariadne/daemon.py        # log_activity at each state transition, LoggerAdapter
src/ariadne/cli.py           # task-timeline command
tests/test_trace.py          # new
```

## Requirements

1. `models.py`: add `trace_id: str` to Task
2. `store.py`:
   - `enqueue_task()` generates trace_id if not passed
   - `retry_task()` copies old.trace_id
   - New table `activity_log` (id, trace_id, task_id, event, details, created_at)
   - `log_activity(trace_id, task_id, event, details=None)` method
   - `get_timeline(trace_id)` returns activity_log rows ordered by created_at
3. `orchestrator.py`: child tasks get leader's trace_id; call log_activity at delegation
4. `daemon.py`: call log_activity at claim/start/complete/fail; use LoggerAdapter with trace_id
5. `cli.py`: `ariadne task-timeline <task_id>` prints timeline
6. SQLite migration: add trace_id column + activity_log table + index

## Verification

```bash
ruff check src/ariadne/
pytest tests/test_trace.py tests/ -v
```

### test_trace.py must cover:
- trace_id generated on enqueue
- trace_id inherited on retry
- trace_id inherited on delegation (orchestrator child task)
- activity_log records state transitions
- get_timeline returns events in order
- task-timeline CLI outputs events
