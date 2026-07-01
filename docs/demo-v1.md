# Ariadne Managed Agent Team Runtime v1 Demo

This demo is the clean-checkout path for validating the local managed-agent
runtime without provider credentials.

## Run

```bash
uv sync --extra dev
uv run ariadne demo-v1 --reset
```

The command writes an isolated SQLite database under `.ariadne-demo-v1/` and
prints the inspection commands. It creates:

- AgentProfiles, a Skill binding, a Squad, and an Issue.
- RuntimeMachine and RuntimeCapability facts from the daemon.
- TaskRuns, RuntimeLeases, IssueTimeline events, and LeaderDecisions.
- A BenchmarkRun whose metrics are computed from product facts.

No live provider is used. Real provider execution is a separate path: git
targets run in isolated worktrees by default, and direct target writes require
`--write-workspace`.

## Verify

```bash
export ARIADNE_DB=.ariadne-demo-v1/ariadne-v1.db
uv run ariadne runtime-list
uv run ariadne capability-list
uv run ariadne taskrun-list
uv run ariadne runtime-lease-list
uv run ariadne leader-decision-list
uv run ariadne benchmark-list
uv run ariadne issue-list
```

Start the dashboard/API:

```bash
export ARIADNE_DB=.ariadne-demo-v1/ariadne-v1.db
uv run ariadne api-serve
```

Then open `http://127.0.0.1:8766/`, or inspect:

```bash
curl http://127.0.0.1:8766/api/runtime-machines
curl http://127.0.0.1:8766/api/runtime-capabilities
curl http://127.0.0.1:8766/api/runtime-leases
curl http://127.0.0.1:8766/api/leader-decisions
curl http://127.0.0.1:8766/api/benchmark-runs
```

## Expected States

- `dry-run`: completed; no external command is spawned.
- `live-execution`: skipped in this demo.
- `blocked`: zero in the happy-path demo; real execution without policy grants is
  recorded as `policy_blocked`.
- `failed`: zero in the happy-path demo.
- `successful`: the demo Issue is `done`, and the BenchmarkRun summary has
  `success=true`.

## Test Commands

```bash
uv run pytest tests/test_clean_checkout_demo.py tests/test_benchmark_run_product_facts.py tests/test_execution_policy.py -q
uv run pytest -q
uv run ruff check src tests
```
