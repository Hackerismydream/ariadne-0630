# Trace & Observability

> Adds trace_id to Task, propagates through claim → delegate → execute → complete.

## Purpose

Every task gets a `trace_id` at creation. All log lines, progress events, and
DB records for that task carry the same trace_id. This enables debugging
"what happened to task X" by grepping one ID.

## Design

### trace_id field on Task

```python
# models.py — add to Task
trace_id: str  # generated at creation, inherited by retry children
```

### Generation & inheritance

- `store.enqueue_task()` generates `trace_id = f"trace-{uuid4().hex[:12]}"`
- `store.retry_task()` copies `old.trace_id` to the new task
- Child tasks created by orchestrator delegation inherit the parent leader task's `trace_id`

### Log propagation

```python
# daemon.py / orchestrator.py
logger = logging.LoggerAdapter(base_logger, {"trace_id": task.trace_id})
logger.info("claimed task %s", task.id)
# Output: INFO ariadne.daemon [trace-abc123] claimed task task-xyz
```

### CLI: task timeline

```bash
ariadne task-timeline <task_id>
```

Prints all events for a task's trace_id in chronological order:
- task created (queued)
- task claimed
- task started (running)
- progress events
- task completed/failed
- child task created (if delegation)
- retry task created (if retry)

### SQLite

```sql
ALTER TABLE task ADD COLUMN trace_id TEXT;
CREATE INDEX idx_task_trace ON task(trace_id);
```

### activity_log table (new)

```sql
CREATE TABLE activity_log (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    task_id TEXT,
    event TEXT NOT NULL,     -- created, claimed, started, progress, completed, failed, delegated, retried
    details TEXT,            -- JSON
    created_at TEXT NOT NULL
);
CREATE INDEX idx_activity_trace ON activity_log(trace_id);
```

`store.log_activity(trace_id, task_id, event, details)` writes here.
Daemon and orchestrator call it at each state transition.

## Tests

- `test_trace_id_generated`: enqueue_task → task has trace_id starting with "trace-"
- `test_trace_id_inherited_on_retry`: retry → child has same trace_id as parent
- `test_trace_id_inherited_on_delegation`: orchestrator delegates → child task has leader's trace_id
- `test_activity_log_records_transitions`: claim → start → complete → 3 activity_log rows
- `test_task_timeline`: create + claim + complete → timeline shows 3 events in order
