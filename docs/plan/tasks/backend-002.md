id: backend-002
scope: backend
status: pending
depends-on: [backend-001]
```

## Objective

Add retry-on-failure integration with real backends, progress reporting
during execution, and verify the full squad loop works with real Codex/Claude
execution (gated). This task wires the failure classification + retry chain
into the daemon's member execution path with real backend latency.

## Context

- Design doc: [docs/architecture/harness-backend.md](../../architecture/harness-backend.md) — progress reporting
- Design doc: [docs/architecture/task-state-machine.md](../../architecture/task-state-machine.md) — failure classification, retry
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/daemon.py          # modified: improved retry with real backends
src/ariadne/backends.py        # modified: progress callback during subprocess
tests/test_backend_integration.py  # new
```

## Requirements

### Progress during execution

CodexBackend and ClaudeBackend should call `on_progress` at key points:
1. Before subprocess starts: "starting execution"
2. After subprocess completes: "execution finished (exit_code=N)"

This is lightweight — we're not parsing streaming output, just bracketing
the subprocess call with progress events.

### Retry with real backends

The daemon's `_maybe_retry` already exists. Verify it works correctly
with real backend failures:
- `agent_error` (non-zero exit) → retry
- `timeout` → retry with longer timeout? (no — same timeout, but retry)
- After max_attempts → no retry, task stays failed

### CLI: agent can specify backend

Update `cli.py agent-create` to accept `--backend codex` or `--backend claude-code`.
The daemon already reads `agent.backends[0]` — no daemon change needed, just
verify CLI passes it through.

### Daemon: pass DelegationDecision handoff_prompt to member task

Currently `_execute_member_task` uses a generic handoff prompt. When the
task was created by a DelegationDecision, the handoff_prompt should come
from the decision. Store it on the task (via a `handoff` column or a
sidecar JSON file) and use it in ExecutionContext.

Simplest approach: add a `handoff_prompt` TEXT column to the task table.
When orchestrator creates a child task, store the decision's handoff_prompt.
When daemon builds ExecutionContext, use task.handoff_prompt if available.

### SQLite migration

```sql
ALTER TABLE task ADD COLUMN handoff_prompt TEXT;
```

Use `CREATE TABLE IF NOT EXISTS` pattern — check if column exists before
adding (SQLite doesn't have IF NOT EXISTS for ALTER TABLE).

### Constraints

- No new dependencies
- Existing 87 tests must still pass
- Real execution is always gated (safety gate in backends)
- Tests use DryRunBackend or mocked subprocess — no real CLI calls in tests

## Verification

```bash
ruff check src/ariadne/backends.py src/ariadne/daemon.py src/ariadne/store.py
pytest tests/ -v  # all existing + new tests must pass
```

### test_backend_integration.py must cover:

- `test_progress_called_during_execution`: DryRunBackend calls on_progress
- `test_retry_with_real_failure`: FailingBackend → retry creates new task
- `test_handoff_prompt_stored_on_task`: orchestrator child task has handoff_prompt
- `test_daemon_uses_handoff_prompt`: ExecutionContext uses task.handoff_prompt when available
- `test_agent_backend_from_cli`: cli agent-create --backend codex → agent.backends == ["codex"]
- `test_squad_loop_with_failing_member`: member fails → retry → second attempt also fails → issue not done
