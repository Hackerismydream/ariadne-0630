# Module Decomposition Analysis

## Modules

| Module | Responsibility | Dependencies | Lines (target) |
|--------|---------------|--------------|----------------|
| models.py | Pydantic data models + enums | none | 250 |
| store.py | SQLite persistence + state transitions | models | 300 |
| daemon.py | Poll loop, claim, heartbeat, cancel | store, models | 350 |
| briefing.py | Generate SquadBriefing from squad+members | store, models | 150 |
| orchestrator.py | Leader delegation + event loop + LangGraph | store, briefing, models | 400 |
| backends.py | ExecutionBackend protocol + Codex/Claude | models | 400 |
| api.py | FastAPI thin layer (optional) | store, orchestrator | 250 |
| cli.py | Typer CLI entry point | store, orchestrator, daemon, backends | 200 |

## Dependency Graph (creation/call order)

```
models.py ◄── no deps (pure Pydantic)
   │
   ├── store.py (imports models)
   │      │
   │      ├── daemon.py (imports store, models)
   │      │
   │      ├── briefing.py (imports store, models)
   │      │      │
   │      │      └── orchestrator.py (imports store, briefing, models)
   │      │             │
   │      │             └── (uses backends via dependency injection)
   │      │
   │      └── api.py (imports store, orchestrator)
   │
   └── backends.py (imports models only — ExecutionResult uses FailureReason)

cli.py imports: store, orchestrator, daemon, backends
```

**No cycles.** Each layer depends only on layers below it.

## Integration Points

| From → To | What crosses the boundary | Verification |
|-----------|--------------------------|--------------|
| daemon → store | `claim_task()`, `start_task()`, `complete_task()`, `fail_task()` | Integration test: daemon claims and completes a real task |
| orchestrator → store | `enqueue_task()`, `get_squad_leader()`, `count_pending_member_tasks()` | Integration test: delegation creates child task in DB |
| orchestrator → briefing | `generate_briefing(squad_id)` | Unit test: briefing contains correct roster |
| orchestrator → backends | `backend.execute(context)` | Integration test: dry-run backend executes via orchestrator |
| daemon → orchestrator | `handle_task(task)` — daemon calls orchestrator when claiming | Integration test: daemon + orchestrator end-to-end |
| cli → all | CLI commands wire up all layers | E2E test: cli issue create → daemon → done |

## Critical Design Decisions

### CD1: orchestrator does NOT import backends directly

The orchestrator receives a `backend_factory: Callable[[str], ExecutionBackend]`
via dependency injection. This prevents the orchestration layer from depending
on the execution layer — backends can be tested/mocked without touching orchestrator.

### CD2: daemon calls orchestrator, not vice versa

```
daemon poll loop → claim task → if task is leader type: orchestrator.handle_leader_task()
                                   if task is member type: orchestrator.handle_member_task()
```

The orchestrator is stateless — each call processes one task. The event loop
is triggered by daemon callbacks (member task complete → check pending → maybe
enqueue leader task).

### CD3: LangGraph is internal to orchestrator.py

LangGraph's StateGraph is used only inside `handle_leader_task()` to model
the leader's decision flow. It does NOT appear in any public API or type
signature. If LangGraph is removed, only `orchestrator.py` changes.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LangGraph supervisor graph too complex for 1-week W2 | medium | delays Phase 2 | Start with plain if/else, add LangGraph only if it adds value |
| Codex/Claude CLI unstable in real execution | medium | delays Phase 3 | dry-run backend always works; real execution is gated |
| SQLite concurrent claim race condition | low | double execution | `BEGIN IMMEDIATE` + RETURNING clause (tested in W1) |
| Scope creep (adding features past 3000 lines) | high | project doesn't finish | README "non-goals" + line count check in CI |
