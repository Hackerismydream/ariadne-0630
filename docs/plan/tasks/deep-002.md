id: deep-002
scope: backend
status: pending
depends-on: [deep-001]
```

## Objective

Upgrade backends.py: parse Claude JSON output, add worktree isolation, stream progress line-by-line.

## Context

- Design doc: [docs/architecture/deep-execution.md](../../architecture/deep-execution.md)

## Path

```
src/ariadne/models.py        # add metadata field to ExecutionResult
src/ariadne/backends.py      # ClaudeBackend JSON parse, worktree, Popen streaming
src/ariadne/daemon.py        # pass trace_id to ExecutionContext for worktree naming
tests/test_deep_backend.py   # new
```

## Requirements

1. `models.py`: add `metadata: dict | None = None` to ExecutionResult
2. `backends.py` ClaudeBackend:
   - After subprocess, try json.loads(stdout)
   - If valid: extract `result` field → stdout, rest → metadata
   - If invalid: keep raw stdout, metadata=None
3. `backends.py` _ShellBackend:
   - Before execution: if target_repo is git, create worktree at `{repo}/.ariadne-worktrees/{trace_id}`
   - Execute in worktree path
   - After execution: capture diff from worktree
   - Cleanup: `git worktree remove --force`
   - Non-git repo: skip worktree, execute in place
4. `backends.py` _ShellBackend:
   - Replace subprocess.run with subprocess.Popen
   - Read stdout line-by-line, call on_progress per line
   - total=0 (indeterminate)
5. ExecutionContext: add `trace_id: str | None = None` for worktree naming

## Verification

```bash
ruff check src/ariadne/
pytest tests/test_deep_backend.py tests/ -v
```

### test_deep_backend.py must cover:
- Claude JSON parsed correctly (mock stdout)
- Claude JSON parse fallback (garbage stdout)
- Worktree created and cleaned up (git repo)
- Non-git dir: no worktree, no error
- Streaming progress: on_progress called per line (mock Popen)
- Codex plain text: no JSON parse, metadata=None
