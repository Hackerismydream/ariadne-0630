# Deep Execution Backend

## Purpose

`backends.py` is the provider-adapter layer for local coding agents. It keeps
the orchestration system independent from Codex/Claude CLI details and gives
the daemon a uniform `ExecutionResult` containing stdout, stderr, diff,
changed files, test command results, session id, and worktree audit metadata.

The current implementation does four things:

1. Parses Claude Code `--output-format json` into structured fields.
2. Streams stdout line-by-line into progress events.
3. Runs real providers in a detached git worktree by default.
4. Captures diff, changed files, test command result, and `worktree_audit`
   before cleanup.

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

### Isolation-first execution

By default, real provider execution requires a git target and creates a
detached worktree:

```bash
git -C {target_repo} worktree add --detach {worktree_path} HEAD
```

Execute in `{worktree_path}` instead of `{target_repo}`.
After execution, capture diff and changed files from the worktree.
On cleanup:

```bash
git -C {target_repo} worktree remove {worktree_path} --force
```

If the target is not a git repo, the backend blocks unless the caller
explicitly uses `--write-workspace`. If worktree creation fails, execution
stops; it does not silently write to the target checkout.

Execution result metadata includes:

- `target_repo_path`
- `execution_repo_path`
- `worktree_created`
- `write_workspace`
- `isolation_required`
- `rendered_command`
- `command_cwd`
- `patch_captured_before_cleanup`
- `original_repo_clean_after`

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
    metadata: dict | None = None  # parsed provider output + worktree_audit
```

## Tests

- `test_claude_json_parsed`: mock stdout as valid Claude JSON → result.stdout = result field, metadata populated
- `test_claude_json_parse_fallback`: mock stdout as garbage → result.stdout = raw, metadata = None
- `test_git_repo_executes_in_worktree_without_env_or_confirmation`: git repo → execution happens in worktree, original repo remains untouched
- `test_non_git_directory_requires_write_workspace`: non-git dir → blocked unless direct writes are explicit
- `test_worktree_creation_failure_does_not_fallback_to_target_repo`: failed isolation → hard stop
- `test_streaming_progress`: mock Popen → on_progress called per line
- `test_codex_plain_text`: codex stdout = plain text → result.stdout = raw, metadata = None
