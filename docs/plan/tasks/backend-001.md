id: backend-001
scope: backend
status: pending
depends-on: [squad-003]
```

## Objective

Implement real CodexBackend and ClaudeBackend in `src/ariadne/backends.py`,
replacing the stub registry. These backends spawn the actual coding agent CLI
(Codex / Claude Code) as a subprocess, capture stdout/stderr/diff, and report
progress. Safety gates (dual confirmation) are non-negotiable.

## Context

- Design doc: [docs/architecture/harness-backend.md](../../architecture/harness-backend.md) — protocol, command templates, safety gate, diff capture
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 3
- Doc index: [docs/INDEX.md](../../INDEX.md)
- Existing code: `src/ariadne/backends.py` (DryRunBackend + protocol + registry)

## Path

```
src/ariadne/backends.py       # modified: add CodexBackend + ClaudeBackend
tests/test_backends.py        # new
```

## Requirements

### CodexBackend

Adapted from Ariadne `ariadne_ltb/execution.py:341` (proven implementation).

```
Command template: codex exec --cd {target_repo} - < {handoff_file}
Env vars:
  ARIADNE_CODEX_COMMAND_TEMPLATE  (override)
  ARIADNE_CODEX_MODEL             (model override)
  ARIADNE_CODEX_REASONING_EFFORT  (effort override)
```

### ClaudeBackend

Adapted from Ariadne `ariadne_ltb/execution.py:523`.

```
Command template: claude --print --output-format json < {handoff_file}
Env vars:
  ARIADNE_CLAUDE_COMMAND_TEMPLATE  (override)
  ARIADNE_CLAUDE_MODEL             (model override)
  ARIADNE_CLAUDE_EFFORT            (effort override)
```

### Safety Gate (both backends)

```python
def execute(self, context, on_progress=None):
    if os.environ.get("ARIADNE_ENABLE_EXTERNAL_EXECUTION") != "1":
        return _blocked_result(context, "ARIADNE_ENABLE_EXTERNAL_EXECUTION must be 1")
    if not context.confirm_execution:
        return _blocked_result(context, "--confirm-execution is required")
    # ... proceed with subprocess
```

No real execution without both gates. Blocked results have `success=False`,
`failure_reason=EXTERNAL_EXECUTION_BLOCKED`.

### Command Template Rendering

Supported placeholders: `{target_repo}`, `{handoff_file}`, `{task_id}`,
`{model}`, `{effort}`, `{system_prompt}`, `{system_prompt_file}`.

Unknown placeholder → `ValueError`. No silent substitution.

### Diff Capture

After execution (success or fail), capture git state:
```python
def _capture_diff(repo_path: str) -> tuple[str | None, list[str]]:
    # git diff HEAD (unstaged changes)
    # git status --porcelain (changed files list)
    # Non-git dir → (None, [])
```

### Handoff File

Before execution, write the handoff prompt to a temp file. Pass the file path
to the command template via `{handoff_file}`.

### Timeout

Use `subprocess.run(timeout=context.timeout_seconds)`. On timeout:
`failure_reason=TIMEOUT`.

### Registry Update

```python
_BACKENDS = {
    "dry-run": DryRunBackend(),
    "codex": CodexBackend(),
    "claude-code": ClaudeBackend(),
}
```

### Constraints

- backends.py imports: models, os, subprocess, shutil, tempfile, pathlib, re, time, logging
- No new dependencies (no openai, no langchain — pure subprocess)
- DryRunBackend and protocol remain unchanged
- Blocked results use `FailureReason.AGENT_ERROR` (no new enum value — keep it simple)

## Verification

```bash
ruff check src/ariadne/backends.py
pytest tests/test_backends.py tests/test_daemon.py -v
```

### test_backends.py must cover:

- `test_safety_gate_blocks_without_env`: no ARIADNE_ENABLE_EXTERNAL_EXECUTION → blocked
- `test_safety_gate_blocks_without_confirm`: no confirm_execution → blocked
- `test_safety_gate_blocks_with_only_env`: env set but no confirm → blocked
- `test_command_template_rendering`: all supported placeholders render correctly
- `test_unknown_placeholder_raises`: unknown {foo} → ValueError
- `test_diff_capture_git_repo`: git repo with changes → diff + changed_files populated
- `test_diff_capture_no_git`: non-git directory → (None, [])
- `test_diff_capture_clean_repo`: git repo with no changes → (None, [])
- `test_backend_registry`: "codex" → CodexBackend, "claude-code" → ClaudeBackend, unknown → ValueError
- `test_codex_is_available`: `codex` on PATH → True, else False (mock which)
- `test_claude_is_available`: `claude` on PATH → True, else False (mock which)
- `test_dry_run_still_works`: existing DryRunBackend behavior unchanged
