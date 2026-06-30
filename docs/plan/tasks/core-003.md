id: core-003
scope: core
status: done
depends-on: [core-002]
```

## Objective

Implement `src/ariadne/daemon.py`: the poll-claim-execute loop that turns
the state machine into a running system. After this task, `cli daemon start`
can poll for queued tasks, claim them, execute via dry-run backend, and
report completion.

## Context

- Design doc: [docs/architecture/task-state-machine.md](../../architecture/task-state-machine.md) — stale claim recovery
- Design doc: [docs/architecture/harness-backend.md](../../architecture/harness-backend.md) — ExecutionBackend protocol, progress
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 3
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/daemon.py
src/ariadne/cli.py          # minimal: issue create + daemon start + daemon status
tests/test_daemon.py
```

## Requirements

### Daemon class

```python
class Daemon:
    def __init__(
        self,
        store: Store,
        backend_factory: Callable[[str], ExecutionBackend],
        runtime_id: str = "local",
        poll_interval: float = 3.0,
        heartbeat_interval: float = 15.0,
        stale_claim_timeout: float = 60.0,
    ): ...

    def start(self, max_iterations: int | None = None) -> None: ...
    def stop(self) -> None: ...

    def _poll_once(self) -> bool: ...  # returns True if claimed a task
    def _execute_task(self, task: Task) -> None: ...
    def _recover_stale_claims(self) -> int: ...
    def _send_heartbeat(self) -> None: ...
```

### Poll Loop

```
start():
  loop:
    1. recover_stale_claims (claimed but no heartbeat for stale_claim_timeout → back to queued)
    2. claim_task for any agent in this runtime
    3. if claimed: execute_task (start → backend.execute → complete/fail)
    4. if no task: sleep(poll_interval)
    5. send heartbeat
    6. if max_iterations reached: stop
```

### Execute Task

```
_execute_task(task):
  store.start_task(task.id)  # claimed → running
  backend = backend_factory(task.backend_name or "dry-run")
  context = ExecutionContext.from_task(task)
  try:
    result = backend.execute(context, on_progress=self._on_progress)
    if result.success:
      store.complete_task(task.id, result.model_dump())
    else:
      store.fail_task(task.id, result.error, result.failure_reason)
      if task.attempt < task.max_attempts:
        store.retry_task(task.id)
  except TimeoutError:
    store.fail_task(task.id, "execution timed out", FailureReason.TIMEOUT)
    store.retry_task(task.id)
```

### Stale Claim Recovery

Scan for tasks in `claimed` status where `dispatched_at` is older than
`stale_claim_timeout`. Set them back to `queued` with `failure_reason =
runtime_recovery`. This handles daemon crash recovery.

### Heartbeat

Write current timestamp to a `daemon_state` table (or a simple key-value in
SQLite). This is for stale detection — no external server to ping.

### CLI (minimal, in cli.py)

```
ariadne issue create --title "..." --assignee-type agent --assignee-id <id>
ariadne issue list
ariadne daemon start [--max-iterations N] [--poll-interval 3]
ariadne daemon status
ariadne agent create --name "..." --backend codex
ariadne agent list
```

daemon start uses DryRunBackend by default. Real backends come in Phase 3.

### Constraints

- daemon.py imports: store, models, backends (only the protocol + DryRunBackend), time, logging
- The poll loop must handle KeyboardInterrupt cleanly (mark running task as failed with runtime_recovery)
- No threads, no asyncio — simple synchronous loop (simpler to reason about, sufficient for local single-user)
- cli.py uses typer, imports store/daemon/models

## Verification

```bash
ruff check src/ariadne/daemon.py src/ariadne/cli.py
pytest tests/test_daemon.py -v
```

### test_daemon.py must cover:

- `test_poll_claims_queued_task`: enqueue task → poll_once → task becomes claimed
- `test_execute_completes_task`: claimed task → execute → completed with result
- `test_execute_fails_task`: backend returns failure → task failed with reason
- `test_retry_on_failure`: fail + attempt < max → new queued task created
- `test_no_retry_when_exhausted`: fail + attempt == max → no new task
- `test_stale_claim_recovery`: old claimed task → recovered to queued
- `test_keyboard_interrupt`: running task → interrupt → task marked failed with runtime_recovery
- `test_heartbeat_updates_state`: after poll → daemon_state timestamp updated
- `test_dry_run_backend_default`: no backend specified → uses DryRunBackend
- `test_cli_issue_create`: cli creates issue visible in list
- `test_cli_daemon_start_max_iterations`: daemon start --max-iterations 1 → runs once and exits
