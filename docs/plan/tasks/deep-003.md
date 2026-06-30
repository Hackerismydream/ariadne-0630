id: deep-003
scope: squad
status: pending
depends-on: [deep-001, deep-002]
```

## Objective

Run real squad loop with LLM delegation + Codex/Claude execution. Add `ariadne squad-run` CLI command.

## Context

- Design doc: [docs/architecture/real-squad.md](../../architecture/real-squad.md)

## Path

```
src/ariadne/cli.py           # squad-run command
src/ariadne/llm_decide.py    # improve prompt with member results on re-evaluation
tests/test_real_squad.py     # new (uses mock backend, not real CLI)
```

## Requirements

1. `cli.py`: `ariadne squad-run --target-repo <path> --confirm-execution`
   - Creates leader + 2 members (coder/codex, tester/claude-code)
   - Creates squad + issue
   - Enqueues leader task
   - Starts daemon with orchestrator + real backends
   - Prints trace timeline at end
2. `llm_decide.py`: on re-evaluation, include completed member results in prompt
   - Pass `completed_results: list[dict]` to llm_decide
   - Prompt: "Members completed: {results}. Decide next step."
3. `llm_decide.py`: if LLM returns invalid JSON 3 times, fall back to deterministic_decide
4. `orchestrator.py`: pass completed member results to llm_decide on re-evaluation

## Verification

```bash
ruff check src/ariadne/
pytest tests/test_real_squad.py tests/ -v
```

### test_real_squad.py must cover (mock backends, not real CLI):
- Squad with 2 members: leader delegates to first, then second, then marks done
- LLM returns garbage 3x → deterministic fallback
- Trace propagation: all tasks share trace_id
- Member results included in re-evaluation prompt
