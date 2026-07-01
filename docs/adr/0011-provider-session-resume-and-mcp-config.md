# ADR 0011: Provider Session Resume and MCP Config Injection

Date: 2026-07-01

## Status

Accepted.

## Context

A managed coding-agent runtime should not treat every TaskRun as an isolated
chat with no memory of prior attempts. Retries and same-trace follow-ups often
need provider context: the previous command output, intermediate reasoning,
or a provider session handle.

Ariadne also needs to pass MCP configuration into provider CLIs. MCP config is
not a global property of the runtime alone: different durable teammate profiles
may need different tool surfaces. A process-level environment variable is a
useful fallback, but it is too blunt as the primary source of truth.

## Decision

Add first-class execution inputs:

- `ExecutionContext.resume_session_id`
- `ExecutionContext.mcp_config_path`

The daemon resolves `resume_session_id` from the parent TaskRun first, then
from the latest completed TaskRun sharing the same `trace_id` whose result
contains `session_id` or `metadata.session_id`.

For MCP config, agent-profile policy wins:

1. `AgentProfile.runtime_policy["mcp_config_path"]`
2. `ARIADNE_MCP_CONFIG`
3. no MCP config

Backends render provider-specific fragments only when values exist. Codex gets
`--mcp-config`; Claude Code gets `--resume` and `--mcp-config`.

## Consequences

Retries can continue a provider session when the backend exposes one, without
making session continuity mandatory for dry-run or providers that do not return
session handles.

MCP configuration becomes a durable teammate capability rather than an ambient
process assumption. The fallback environment variable remains useful for local
experiments, but it does not override profile policy.

## Evidence

- `src/ariadne/models.py` carries `resume_session_id` and `mcp_config_path` on
  `ExecutionContext`.
- `src/ariadne/daemon.py` resolves provider sessions and applies the
  profile-over-environment MCP precedence.
- `src/ariadne/backends.py` renders `{resume_session_id}` and `{mcp_config}`
  placeholders and adds provider-specific command fragments.
- `tests/test_deep_backend.py` covers resume and MCP fragment rendering.
