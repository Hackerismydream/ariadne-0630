# Real Squad Orchestration

> Validates the full leader→delegate→member→execute→re-evaluate loop with real Codex/Claude backends.

## Purpose

Current squad E2E test uses DryRunBackend. This task runs the real squad loop:
leader uses LLM to decide delegation, member uses Codex/Claude to execute real
code changes, event loop re-activates leader to evaluate results.

## Design

### Scenario: multi-step feature implementation

Squad: 1 leader + 2 members (Coder with codex backend, Tester with claude-code backend)

Issue: "Add a string_utils module with capitalize and reverse functions, plus tests"

Expected flow:
1. Leader activated → LLM decides: delegate capitalize to Coder, reverse to Coder
2. Coder (Codex) executes: adds capitalize function
3. Leader re-activated → sees Coder completed → delegates reverse
4. Coder (Codex) executes: adds reverse function
5. Leader re-activated → all coding done → delegates test writing to Tester
6. Tester (Claude) executes: adds test file
7. Leader re-activated → all members done → marks issue done

### CLI: squad run

```bash
ariadne squad-run --target-repo /path/to/repo
# Direct target writes, only when intentionally bypassing worktree isolation:
ariadne squad-run --target-repo /path/to/repo --write-workspace
```

Creates the squad, issue, leader task, and runs the daemon with orchestrator.
Prints trace timeline at the end.

### LLM decision quality

The LLM receives:
- Full briefing (protocol + roster with skills + instructions)
- Issue title + description
- On re-evaluation: results of completed member tasks

The LLM must output valid DelegationDecision JSON. If it fails 3 times,
fall back to deterministic_decide.

### Concurrency note

Ariadne is single-worker serial. Members execute one at a time.
This is a documented limitation — not a bug. The value is in the orchestration
pattern, not parallelism.

## Tests

- `test_squad_real_codex`: leader delegates → Codex member executes → real code change in dogfood repo
- `test_squad_multi_step`: leader delegates twice (two functions) → both execute → issue done
- `test_squad_llm_failure_fallback`: LLM returns garbage 3 times → falls back to deterministic
- `test_squad_trace_propagation`: all tasks in the squad loop share the same trace_id
