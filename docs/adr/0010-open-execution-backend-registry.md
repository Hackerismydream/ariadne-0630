# ADR 0010: Open Execution Backend Registry

Date: 2026-07-01

## Status

Accepted.

## Context

Ariadne needs to run several coding-agent harnesses behind one
`ExecutionBackend` protocol. The first implementation could have kept a fixed
`_BACKENDS = {"dry-run": ..., "codex": ..., "claude-code": ...}` literal and
treated backend selection as an internal switch.

That works for three built-ins, but it makes local benchmark harnesses,
synthetic providers, and tests edit production registry code whenever they need
a new backend name. At the same time, full third-party package discovery is
premature: there are no external backend authors yet, no stable package API,
and no reason to add entry-point discovery just to look extensible.

## Decision

Use an in-process registry seam:

- `register_backend(backend)`
- `get_backend(name)`
- `available_backends()`

Built-in dry-run, Codex, and Claude Code backends register through the same
function that tests and local benchmark providers use. Duplicate names fail
fast with `ValueError`.

Do not implement Python entry-point discovery yet.

## Consequences

This keeps the execution layer open for local extension without turning the
project into a plugin platform before there are plugin users.

The registry is still process-local. A caller that wants a custom backend must
register it before requesting it. That is enough for tests, synthetic benchmark
providers, and dogfooding, and it avoids committing to a public distribution
contract.

## Evidence

- `src/ariadne/backends.py` defines the registry functions and registers the
  built-ins at import time.
- `tests/test_backends.py` covers duplicate registration and custom in-process
  backend registration.
- `docs/plan/backlog.md` keeps third-party entry-point discovery deferred until
  real external backend demand exists.
