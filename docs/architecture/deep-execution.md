# Deep Execution Backend

> Upgrades backends.py from subprocess wrapper to structured output parsing + worktree isolation + streaming progress.

## Purpose

Current backends.py is `subprocess.run(shell=True)` + capture stdout as string. This task makes it real:
1. Parse Claude's `--output-format json` into structured fields
2. Isolate execution in a git worktree (no pollution of target repo)
3. Stream stdout line-by-line for real progress updates

## Design

### Claude JSON output parsing

Claude Code with `--output-format json` returns:
```json
{
  "type": "result",
  "result": "text output...",
  "session_id": "...",
  "num_turns": 3,
  "cost_usd": 0.01
}
```

`ClaudeBackend.execute()` parses this JSON from stdout and populates:
- `ExecutionResult.stdout` — the `result` field (not raw JSON)
- `ExecutionResult.metadata` — `{"session_id": ..., "num_turns": ..., "cost_usd": ...}`

If JSON parse fails, fall back to raw stdout (graceful degradation).

### Codex output

Codex `exec` outputs plain text to stdout. No structured parsing needed.
`ExecutionResult.stdout` = raw stdout as before.

### Worktree isolation

Before execution, create a git worktree:
```bash
git -C {target_repo} worktree add {worktree_path} -b ariadne/{trace_id}
```

Execute in `{worktree_path}` instead of `{target_repo}`.
After execution, capture diff from the worktree.
On cleanup: `git -C {target_repo} worktree remove {worktree_path} --force`

If target_repo is not a git repo: skip worktree, execute in place (graceful).

### Streaming progress

Replace `subprocess.run` with `subprocess.Popen` + line-by-line read:
```python
proc = subprocess.Popen(command, stdout=PIPE, stderr=PIPE, text=True, ...)
for line in proc.stdout:
    on_progress(ProgressUpdate(summary=line.strip()[:200], step=..., total=...))
```

`total` is unknown for streaming — use `total=0` to indicate "indeterminate".

## ExecutionResult changes

```python
class ExecutionResult(BaseModel):
    # existing fields...
    metadata: dict | None = None  # parsed structured output (Claude JSON, etc.)
```

## Tests

- `test_claude_json_parsed`: mock stdout as valid Claude JSON → result.stdout = result field, metadata populated
- `test_claude_json_parse_fallback`: mock stdout as garbage → result.stdout = raw, metadata = None
- `test_worktree_isolation`: git repo → execution happens in worktree, diff captured from worktree
- `test_worktree_non_git`: non-git dir → executes in place, no error
- `test_streaming_progress`: mock Popen → on_progress called per line
- `test_codex_plain_text`: codex stdout = plain text → result.stdout = raw, metadata = None
