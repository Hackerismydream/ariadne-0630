# deep-005: Agent Platform Foundations

## Context

Ariadne is being shaped as a real local managed-agent runtime, not a demo.
The SaaS shell around Multica remains out of scope: RAG, multi-client apps,
Lark/GitHub integrations, tenants, auth, billing, scheduled autopilot, and a
LangGraph supervisor are not part of this slice.

This task fills the engineering substrate that was previously easy to overstate
in docs or resume bullets:

- backend extensibility without editing a registry literal
- benchmark comparison that labels dry-run numbers as simulated
- provider session continuity across retry/same-trace TaskRuns
- MCP config injection into execution backends
- first-class Skill records that materialize into handoffs
- Skill verification commands recorded as evidence rather than hard gates
- WAL-enabled file-backed SQLite stores

## Delivered Scope

### Backend registry

`src/ariadne/backends.py` exposes:

- `register_backend(backend)`
- `get_backend(name)`
- `available_backends()`

Built-in dry-run, Codex, and Claude Code backends register through the same API.
Duplicate names fail fast with `ValueError`.

Third-party package discovery via entry points is intentionally not implemented;
that belongs to a later public productization phase when external backend
authors exist.

### Truthful benchmark comparison

`ariadne benchmark-compare` compares serial and bounded-parallel execution.

Dry-run mode injects deterministic latency and reports `simulated: true`.
Real backend mode reports `blocked` unless `ARIADNE_ENABLE_EXTERNAL_EXECUTION=1`
is set. This prevents dry-run acceleration from being presented as real Codex or
Claude Code performance.

### Resume and MCP execution inputs

`ExecutionContext` now carries:

- `resume_session_id`
- `mcp_config_path`

The daemon resolves `resume_session_id` from the parent TaskRun or the latest
completed TaskRun with the same `trace_id`. Agent-profile
`runtime_policy["mcp_config_path"]` wins over `ARIADNE_MCP_CONFIG`.

Codex adds `--mcp-config {mcp_config}` when an MCP config is present. Claude
Code adds `--resume {resume_session_id}` and `--mcp-config {mcp_config}` only
when those values exist.

### Skill capability packages

Skills persist routing description, prompt snippet, allowed tools, verification
command, source path, and version. Bound skills materialize into TaskRun
handoffs as a `Skill capability package`, so `skill_refs` are no longer only
labels.

After successful backend execution, skill verification commands run in the
execution repo path when available. The result is recorded in:

- TaskRun result metadata
- `activity_log` as `verification_passed` or `verification_failed`
- IssueTimeline as `tests_reported`

Verification failure does not directly fail the TaskRun; it is evidence for
leader re-evaluation and follow-up routing.

## Non-goals

- No third-party backend entry-point discovery.
- No generated real backend performance numbers without running real providers.
- No SaaS shell: RAG, multi-client UI, Lark/GitHub integrations, tenants, auth,
  billing, scheduled autopilot, or LangGraph supervisor.
- No public packaging push such as LICENSE/CONTRIBUTING/uvx docs in this slice.

## Verification

Focused checks:

```bash
uv run pytest tests/test_backends.py tests/test_eval.py tests/test_agent_profile_skill.py tests/test_daemon.py -q
```

Full checks before landing:

```bash
uv run ruff check src tests
uv run pytest -q
uv run ariadne benchmark-compare --tasks 2 --backend dry-run --max-concurrent 2 --task-duration 0.01
```
