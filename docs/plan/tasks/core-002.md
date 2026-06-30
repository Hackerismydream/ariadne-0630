id: core-002
scope: core
status: pending
depends-on: [core-001]
```

## Objective

Implement `src/ariadne/store.py`: SQLite persistence layer with atomic state
transitions. This is the control plane — all task lifecycle operations go
through this module.

## Context

- Design doc: [docs/architecture/task-state-machine.md](../../architecture/task-state-machine.md) — states, transitions, schema, atomic claim
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 1
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/store.py
tests/test_store.py
tests/test_state_machine.py
```

## Requirements

### Store class

```python
class Store:
    def __init__(self, db_path: str = "ariadne.db"): ...

    # --- Issue ---
    def create_issue(self, title, description, assignee_type, assignee_id) -> Issue: ...
    def get_issue(self, issue_id: str) -> Issue | None: ...
    def list_issues(self) -> list[Issue]: ...
    def update_issue_status(self, issue_id: str, status: IssueStatus) -> Issue: ...

    # --- Task ---
    def enqueue_task(self, issue_id, agent_id, squad_id=None) -> Task: ...
    def claim_task(self, agent_id: str, runtime_id: str) -> Task | None: ...
    def start_task(self, task_id: str) -> Task: ...
    def complete_task(self, task_id: str, result: dict) -> Task: ...
    def fail_task(self, task_id: str, error: str, reason: FailureReason) -> Task: ...
    def cancel_task(self, task_id: str) -> Task: ...
    def retry_task(self, task_id: str) -> Task: ...  # creates new task
    def get_task(self, task_id: str) -> Task | None: ...
    def get_pending_member_tasks(self, squad_id: str) -> list[Task]: ...

    # --- Agent ---
    def create_agent(self, name, instructions, backends, skills) -> Agent: ...
    def get_agent(self, agent_id: str) -> Agent | None: ...
    def list_agents(self) -> list[Agent]: ...

    # --- Squad ---
    def create_squad(self, name, leader_id, instructions="") -> Squad: ...
    def add_squad_member(self, squad_id, member_id, role) -> SquadMember: ...
    def get_squad(self, squad_id: str) -> Squad | None: ...
    def get_squad_members(self, squad_id: str) -> list[SquadMember]: ...
    def get_squad_leader(self, squad_id: str) -> Agent: ...
```

### State Transition Enforcement

Every state-changing method must:
1. Load current task from DB
2. Check if transition is legal (per the legal transitions table in design doc)
3. If illegal → raise `InvalidStateTransition(current_status, attempted_action)`
4. If legal → UPDATE in a transaction, set timestamps appropriately

### Atomic Claim

`claim_task` must use `BEGIN IMMEDIATE` to serialize concurrent claims.
See design doc for the exact SQL pattern.

### Retry Logic

`retry_task`:
- Load the failed task
- Check `attempt < max_attempts` (else raise `MaxAttemptsExhausted`)
- Create a NEW task with `attempt = old.attempt + 1`, `parent_task_id = old.id`
- The new task status is `queued`

### SQLite Schema

Per design doc `task-state-machine.md`. Create tables on `__init__` if not exist
(using `CREATE TABLE IF NOT EXISTS`).

### Constraints

- store.py imports only: `sqlite3`, `models`, `datetime`, `json`, `uuid`
- No ORM (no SQLAlchemy — raw sqlite3 for minimalism)
- All writes in transactions
- `InvalidStateTransition` and `MaxAttemptsExhausted` are custom exceptions in store.py

## Verification

```bash
ruff check src/ariadne/store.py
pytest tests/test_store.py tests/test_state_machine.py -v
```

### test_state_machine.py must cover (per design doc "Tests Required"):

- `test_legal_transitions`: queued→claimed→running→completed
- `test_legal_transitions_failed`: queued→claimed→running→failed
- `test_illegal_transitions`: completed→running, failed→completed, queued→running (skip claimed)
- `test_atomic_claim`: two concurrent claim_task calls → only one gets the task
- `test_retry_creates_new_task`: retry creates new task with parent_task_id set, attempt incremented
- `test_max_attempts_exhausted`: after max_attempts failures, retry raises MaxAttemptsExhausted
- `test_failure_classification`: each FailureReason stored correctly
- `test_stale_claim_recovery`: claimed task with old dispatched_at → recovery to queued

### test_store.py must cover:

- create_issue + get_issue round-trip
- create_agent + list_agents
- create_squad + add_squad_member + get_squad_members
- enqueue_task creates task with status=queued
- complete_task sets result and completed_at
- get_pending_member_tasks returns only non-terminal member tasks
