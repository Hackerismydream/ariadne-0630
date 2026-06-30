id: squad-002
scope: squad
status: pending
depends-on: [squad-001]
```

## Objective

Implement `src/ariadne/orchestrator.py`: the Squad leader delegation +
event loop. This replaces Ariadne's 1779-line waterfall orchestrator with
a multica-style event-driven model: leader claims → reads briefing → outputs
DelegationDecision → child task created → leader stops → member completes →
event loop re-activates leader.

## Context

- Design doc: [docs/architecture/squad-orchestration.md](../../architecture/squad-orchestration.md) — delegation flow, event loop, DelegationDecision model
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 2
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/orchestrator.py
tests/test_orchestrator.py
```

## Requirements

### Orchestrator class

```python
class Orchestrator:
    def __init__(self, store: Store, llm_decide: Callable[..., DelegationDecision | None]): ...

    def handle_leader_task(self, task: Task) -> None:
        """Process one leader activation:
        1. Load squad + generate briefing
        2. Call llm_decide(briefing, issue) → DelegationDecision or None
        3. If DelegationDecision: validate target_agent in roster, create child task
        4. If None: mark issue as done (leader decided no more work)
        5. Mark leader task completed
        """

    def on_member_task_complete(self, task: Task) -> None:
        """Event loop callback when a member task reaches terminal state.
        1. Check if any pending member tasks remain for this squad
        2. If none remain: re-enqueue leader task for evaluation
        3. If some remain: do nothing (wait)
        """
```

### DelegationDecision validation

Before creating a child task, validate:
- `decision.target_agent_id` must be a squad member (in roster)
- `decision.backend` must be a known backend (check via get_backend, catch ValueError)
- If validation fails: mark leader task as failed with `failure_reason=agent_error`

### Child task creation

When leader delegates:
- Create a new task: `store.enqueue_task(issue_id, target_agent_id, squad_id)`
- The child task is `queued` — daemon will pick it up
- Record a comment/activity on the issue (optional, via store)

### Event loop

`on_member_task_complete` is called by the daemon after a member task
reaches terminal state. It checks `store.get_pending_member_tasks(squad_id)`:
- If empty → enqueue a new leader task (so leader re-evaluates)
- If non-empty → return (wait for remaining members)

### llm_decide callback

The orchestrator does NOT call an LLM directly. It receives a `llm_decide`
callable that takes `(briefing: SquadBriefing, issue: Issue) -> DelegationDecision | None`.
This makes the orchestrator testable without LLM calls.

For Phase 2, provide a `deterministic_decide` function that picks the first
member with a matching backend. Real LLM integration comes in squad-003.

### Constraints

- orchestrator.py imports: store, models, briefing, logging
- No direct LLM imports — llm_decide is injected
- No direct backend imports — only validates backend name via try/except get_backend
- Leader task and member task are both Task records, distinguished by agent_id
  (leader's agent_id vs member's agent_id)

## Verification

```bash
ruff check src/ariadne/orchestrator.py
pytest tests/test_orchestrator.py -v
```

### test_orchestrator.py must cover:

- `test_leader_delegates_to_member`: leader task → llm_decide returns DelegationDecision → child task created for target_agent
- `test_leader_marks_done_when_no_delegation`: llm_decide returns None → issue marked done, leader task completed
- `test_delegation_rejects_unknown_agent`: DelegationDecision with agent_id not in roster → leader task failed
- `test_event_loop_re_activates_leader`: all members complete → new leader task enqueued
- `test_event_loop_waits_for_pending`: some members still running → no new leader task
- `test_deterministic_decide_picks_matching_backend`: deterministic_decide selects member with matching backend
- `test_child_task_is_queued`: created child task has status=queued
- `test_leader_task_completed_after_delegation`: leader task status=completed after delegating
