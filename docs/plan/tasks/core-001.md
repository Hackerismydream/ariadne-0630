id: core-001
scope: core
status: done
depends-on: []
```

## Objective

Create project scaffold + all Pydantic models and enums in `src/ariadne/models.py`.
No SQLite, no logic — just the type definitions that all other modules depend on.

## Context

- Design doc: [docs/architecture/task-state-machine.md](../../architecture/task-state-machine.md)
- Design doc: [docs/architecture/squad-orchestration.md](../../architecture/squad-orchestration.md)
- Design doc: [docs/architecture/harness-backend.md](../../architecture/harness-backend.md)
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md)
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/__init__.py
src/ariadne/models.py
pyproject.toml
tests/__init__.py
tests/test_models.py
```

## Requirements

### pyproject.toml

- Package name: `ariadne`
- Python: >=3.11
- Dependencies: pydantic >=2
- Dev dependencies: pytest, ruff
- CLI entry point: `ariadne = "ariadne.cli:app"` (cli.py not created yet, just declare)
- src/ layout

### models.py — must define these types (all Pydantic BaseModel or Enum)

**Enums:**
- `TaskStatus`: queued, claimed, running, completed, failed, cancelled
- `IssueStatus`: backlog, todo, in_progress, done, cancelled
- `FailureReason`: agent_error, timeout, runtime_offline, runtime_recovery, manual
- `AssigneeType`: agent, squad

**Models (fields per design docs):**
- `Issue`: id, title, description, status, assignee_type, assignee_id, created_at
- `Task`: id, issue_id, agent_id, squad_id, status, attempt, max_attempts, parent_task_id, failure_reason, dispatched_at, started_at, completed_at, result, error, runtime_id, created_at
- `Agent`: id, name, instructions, backends (list[str]), skills (list[str])
- `Squad`: id, name, leader_id, instructions
- `SquadMember`: squad_id, member_type, member_id, role
- `SquadBriefing`: protocol, roster (list[RosterEntry]), instructions
- `RosterEntry`: agent_id, name, role, skills, backends
- `DelegationDecision`: target_agent_id, backend, handoff_prompt, reason, skill_refs
- `ExecutionContext`: task_id, agent_name, agent_instructions, handoff_prompt, target_repo_path, skill_refs, timeout_seconds, confirm_execution, model, effort
- `ExecutionResult`: backend_name, success, exit_code, stdout, stderr, diff, changed_files, test_result, failure_reason, duration_seconds, command
- `ProgressUpdate`: task_id, summary, step, total, timestamp

### Constraints

- models.py imports nothing except `pydantic`, `enum`, `datetime`
- All datetime fields use `datetime` (ISO format in JSON)
- All optional fields explicitly typed `X | None = None`
- No methods on models except `@property` for computed values (e.g., `is_terminal`)

## Verification

```bash
# Type check
ruff check src/ariadne/models.py

# Tests (must pass before merge):
pytest tests/test_models.py -v
```

### test_models.py must cover:

- Every enum has the exact values listed in design docs
- `TaskStatus` has exactly 6 members
- `FailureReason` has exactly 5 members
- `Task` model accepts all required fields, rejects missing required fields
- `Task.attempt` defaults to 1, `max_attempts` defaults to 2
- `DelegationDecision` requires all 5 fields (no defaults)
- `ExecutionResult` with `success=True` has no `failure_reason`
- All models serialize to JSON and deserialize correctly (round-trip)
