# Harness Backend

> Derives from: multica `server/internal/daemon/daemon.go` (runTask),
> `server/internal/daemon/client.go` (progress reporting),
> Ariadne `ariadne_ltb/execution.py` (CodexBackend, ClaudeCodeBackend — proven implementation)

## Purpose

Abstract coding-agent harnesses (Codex CLI, Claude Code CLI) behind a uniform
protocol so the orchestrator can switch backends without code changes, and new
harnesses can be added by implementing one interface.

## Boundary

```
Orchestrator            ExecutionBackend (protocol)
    │                          │
    │  ExecutionContext        │
    ├─────────────────────────►│
    │                          ├── CodexBackend
    │                          │   subprocess.run("codex exec --cd {repo} ...")
    │                          │
    │                          ├── ClaudeBackend
    │  ExecutionResult         │   subprocess.run("claude --print ...")
    │◄─────────────────────────┤
    │                          │
    │  report_progress()       │  (callback, not return)
    │◄─────────────────────────┤
```

The backend knows nothing about squads, issues, or task state. It receives an
`ExecutionContext` and returns an `ExecutionResult`. Progress is reported via
a callback during execution.

## ExecutionBackend Protocol

```python
class ExecutionBackend(Protocol):
    name: str  # "codex" | "claude-code" | "dry-run" | extension name

    def is_available(self) -> bool: ...
    def execute(self, context: ExecutionContext,
                on_progress: Callable[[ProgressUpdate], None] | None = None
    ) -> ExecutionResult: ...
```

## ExecutionContext

```python
class ExecutionContext(BaseModel):
    task_id: str
    agent_name: str
    agent_instructions: str
    handoff_prompt: str           # the actual work instruction
    target_repo_path: str         # absolute path to repo
    skill_refs: list[str]         # skill files to materialize
    timeout_seconds: int = 600
    confirm_execution: bool = False
    model: str | None = None
    effort: str | None = None     # reasoning effort override
    trace_id: str | None = None
    test_command: str | None = None
    test_timeout_seconds: int = 120
    resume_session_id: str | None = None
    mcp_config_path: str | None = None
```

## ExecutionResult

```python
class ExecutionResult(BaseModel):
    backend_name: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    diff: str | None              # git diff after execution
    changed_files: list[str]
    failure_reason: FailureReason | None
    duration_seconds: float
    command: str                  # redacted command for logging
    metadata: dict | None = None
    command_cwd: str | None = None
    execution_repo_path: str | None = None
    test_command: str | None = None
    test_exit_code: int | None = None
    test_stdout: str | None = None
    test_stderr: str | None = None
    test_duration_seconds: float | None = None
    test_passed: bool | None = None
    session_id: str | None = None
```

`session_id` is a first-class resume handle. Backends may also return it inside
`metadata`; `_ShellBackend` copies `metadata["session_id"]` into the field when
available.

## Backends

### CodexBackend

> Adapted from Ariadne `ariadne_ltb/execution.py:341` (proven, has safety gates)

```
Command template: codex exec --cd {target_repo} - < {handoff_file}
Optional fragments:
  --mcp-config {mcp_config}       (only when mcp_config_path is set)
Env vars:
  ARIADNE_CODEX_COMMAND_TEMPLATE  (override)
  ARIADNE_CODEX_MODEL             (model override)
  ARIADNE_CODEX_REASONING_EFFORT  (effort override)
```

### ClaudeBackend

> Adapted from Ariadne `ariadne_ltb/execution.py:523`

```
Command template: claude --print --output-format json < {handoff_file}
Optional fragments:
  --resume {resume_session_id}    (only when resume_session_id is set)
  --mcp-config {mcp_config}       (only when mcp_config_path is set)
Env vars:
  ARIADNE_CLAUDE_COMMAND_TEMPLATE  (override)
  ARIADNE_CLAUDE_MODEL             (model override)
  ARIADNE_CLAUDE_EFFORT            (effort override)
```

## Safety Gate (non-negotiable)

Both backends require **dual confirmation** before any real execution:

```python
def execute(self, context: ExecutionContext, ...) -> ExecutionResult:
    if os.environ.get("ARIADNE_ENABLE_EXTERNAL_EXECUTION") != "1":
        return _blocked("ARIADNE_ENABLE_EXTERNAL_EXECUTION must be 1")
    if not context.confirm_execution:
        return _blocked("--confirm-execution is required")
    # ... proceed with subprocess.run
```

No real execution without both gates. This is carried over from Ariadne and
matches multica's gated execution approach.

## Command Template Rendering

```python
SUPPORTED_PLACEHOLDERS = {
    "target_repo", "execution_repo", "handoff_file", "task_id",
    "model", "effort", "system_prompt", "resume_session_id",
    "mcp_config",
}

def render_command(
    template: str,
    context: ExecutionContext,
    handoff_file: str,
    execution_repo_path: str | None = None,
) -> str:
    """Render command template. Fail fast on unknown placeholder."""
    # ...
```

Unknown placeholders → `ValueError`. No silent substitution.

## Progress Reporting

> Derives from: multica `client.go:238` ReportProgress

```python
class ProgressUpdate(BaseModel):
    task_id: str
    summary: str          # human-readable progress
    step: int             # current step
    total: int            # total steps (estimated)
    timestamp: datetime
```

Backends call `on_progress(ProgressUpdate(...))` during execution. The daemon
layer persists these to IssueTimeline as `progress_reported` events.

## Backend Registry

```python
def register_backend(backend: ExecutionBackend) -> None: ...
def get_backend(name: str) -> ExecutionBackend: ...
def available_backends() -> list[str]: ...
```

Built-ins are registered in-process at import time. The registry is intentionally
not a third-party entry-point discovery mechanism yet; package discovery belongs
to a later open-source productization phase after real external backend authors
exist.

## Session Resume and MCP Config

The daemon fills `resume_session_id` from the latest completed TaskRun in the
same retry chain or trace whose result contains `session_id` or
`metadata.session_id`. Agent-profile `runtime_policy["mcp_config_path"]` wins
over the process-level `ARIADNE_MCP_CONFIG` environment variable.

Both fields are optional and render to provider-specific command fragments only
when present, so dry-run and existing command templates keep their old behavior.

## Diff Capture

After execution completes (success or fail), capture git state:

```python
def capture_diff(repo_path: str) -> tuple[str, list[str]]:
    """Returns (diff_text, changed_files)."""
    # git diff HEAD (unstaged changes made by the agent)
    # git status --porcelain (list of changed files)
```

If repo is not a git repo → `diff=None, changed_files=[]`. No error, just absent.

## Tests Required

| Test | What it verifies |
|------|-----------------|
| `test_safety_gate_blocks_without_env` | No `ARIADNE_ENABLE_EXTERNAL_EXECUTION` → blocked result |
| `test_safety_gate_blocks_without_confirm` | No `confirm_execution` → blocked result |
| `test_command_template_rendering` | All supported placeholders render correctly |
| `test_provider_specific_resume_and_mcp_fragments` | Resume/MCP fragments are conditional |
| `test_unknown_placeholder_fails` | Unknown placeholder → `ValueError` |
| `test_diff_capture` | Git repo with changes → diff + changed_files populated |
| `test_diff_capture_no_git` | Non-git directory → diff=None, no error |
| `test_backend_registry` | Known names return backends, unknown → `ValueError` |
| `test_backend_registry_accepts_in_process_extensions` | Register/get/duplicate behavior for extension backends |
| `test_dry_run_backend` | DryRunBackend returns success without subprocess |
| `test_timeout_handling` | Execution exceeding timeout → `failure_reason=timeout` |
| `test_progress_callback` | on_progress called during execution with valid ProgressUpdate |
