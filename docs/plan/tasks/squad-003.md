id: squad-003
scope: squad
status: pending
depends-on: [squad-002]
```

## Objective

Integrate LangGraph supervisor graph into the orchestrator's `handle_leader_task`,
and wire the orchestrator into the daemon so that squad-assigned issues flow
end-to-end: issue → leader claim → LangGraph delegation → member execution →
event loop → leader re-evaluation → issue done.

This task also adds a `llm_decide` implementation using an LLM (DeepSeek or
equivalent) so the leader can make real delegation decisions based on the
issue content and roster.

## Context

- Design doc: [docs/architecture/squad-orchestration.md](../../architecture/squad-orchestration.md) — LangGraph integration section, graph topology
- Design doc: [docs/architecture/harness-backend.md](../../architecture/harness-backend.md) — backend selection in DelegationDecision
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 2
- Doc index: [docs/INDEX.md](../../INDEX.md)
- Backlog note: "Start with plain if/else, add LangGraph only if it adds value in W2"

## Decision: LangGraph optional, LLM required

Per the backlog risk note, LangGraph adds complexity. For this task:

1. **Required**: implement `llm_decide` that calls an LLM with the briefing +
   issue text, parses the response into a DelegationDecision. Use a simple
   prompt + JSON parse approach (no LangGraph needed for a single decision node).
2. **Optional**: if the plain approach works cleanly, skip LangGraph entirely.
   LangGraph is only useful if we need multi-step leader reasoning (plan →
   delegate → verify). For v1 single-delegation-per-activation, a direct LLM
   call is simpler and more testable.

If LangGraph is skipped, update squad-orchestration.md to reflect this decision.

## Path

```
src/ariadne/llm_decide.py       # LLM-backed delegation decision
src/ariadne/daemon.py           # modified: wire orchestrator into _execute_task
src/ariadne/orchestrator.py     # modified: accept llm_decide from llm_decide.py
tests/test_llm_decide.py
tests/test_squad_e2e.py
```

## Requirements

### llm_decide.py

```python
def make_llm_decide(api_key: str | None = None, model: str = "deepseek-chat") -> Callable:
    """Return a callable (briefing, issue) -> DelegationDecision | None.

    1. Build a prompt from briefing + issue title/description
    2. Call LLM API (OpenAI-compatible, via openai package or httpx)
    3. Parse JSON response into DelegationDecision
    4. Return None if LLM says "no delegation needed"
    5. On API error: return None (graceful degradation)
    """
```

The prompt should:
- Include the Operating Protocol (from briefing.protocol)
- Include the roster (each member's name, role, skills, backends)
- Include the issue title and description
- Ask the LLM to output JSON: `{"target_agent_id": "...", "backend": "...", "handoff_prompt": "...", "reason": "...", "skill_refs": []}`
- Or `{"delegation": "none"}` if no delegation needed

### daemon.py changes

In `_execute_task`, detect whether the task is a leader task or member task:
- If `task.squad_id` is set AND `task.agent_id` is the squad leader → leader task → call `orchestrator.handle_leader_task`
- Otherwise → member task → execute via backend (existing logic)
- After member task completes, call `orchestrator.on_member_task_complete`

### orchestrator.py changes

- Accept an optional `llm_decide` factory. If none provided, use `deterministic_decide`.
- `handle_leader_task` uses the injected `llm_decide`.

### E2E test (test_squad_e2e.py)

Using a fake LLM that returns a fixed DelegationDecision:
1. Create squad (leader + 1 member)
2. Create issue assigned to squad
3. Enqueue leader task
4. Start daemon with --max-iterations
5. Verify: leader task completed, child task created for member, member task executed (dry-run), event loop re-activates leader, leader marks issue done

### Constraints

- llm_decide.py imports: models, json, logging, (openai or httpx)
- No hard dependency on LLM — if api_key is None, fall back to deterministic_decide
- daemon.py changes must not break existing non-squad task execution
- All existing 63 tests must still pass

## Verification

```bash
ruff check src/ariadne/llm_decide.py src/ariadne/daemon.py src/ariadne/orchestrator.py
pytest tests/ -v  # all 63 existing + new tests must pass
```

### test_llm_decide.py must cover:

- `test_llm_decide_returns_delegation`: fake LLM returns valid JSON → DelegationDecision created
- `test_llm_decide_returns_none_for_no_delegation`: fake LLM returns {"delegation": "none"} → None
- `test_llm_decide_graceful_on_api_error`: API raises → returns None (no crash)
- `test_llm_decide_fallback_without_key`: api_key=None → uses deterministic_decide

### test_squad_e2e.py must cover:

- `test_squad_full_loop`: leader → delegate → member execute → leader re-eval → done (with fake LLM)
