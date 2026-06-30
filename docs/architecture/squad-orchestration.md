# Squad Orchestration

> Derives from: multica `server/internal/handler/squad_briefing.go`,
> `server/internal/handler/issue_trigger.go`
> Source files read: squad_briefing.go (284 lines), issue_trigger.go, daemon.go (handleTask/runTask)

## Purpose

Multi-agent collaboration via Squad: a leader agent coordinates members by
delegation, members execute independently, an event loop re-evaluates after
each member completes. The leader **never does implementation work** — this
is the core protocol rule from multica's `squadOperatingProtocol`.

## Multica's Mechanism (what we extract)

```
multica squad_briefing.go:
  1. issue assigned to squad → task enqueued for leader
  2. leader claims → injected with 3-section briefing:
     a. Squad Operating Protocol (hardcoded rules: coordinate, don't implement)
     b. Squad Roster (member name + type + role + skills + mention markdown)
     c. Squad Instructions (user-defined)
  3. leader posts ONE delegation comment @mentioning chosen member(s)
  4. mention triggers a new task for each mentioned member
  5. leader records evaluation (action/no_action/failed) → STOP
  6. member completes → event loop re-triggers leader → goto 2
```

## Our Mechanism (what we change)

```
ariadne orchestrator.py:
  1. issue assigned to squad → task enqueued for leader
  2. leader claims → briefing generated (same 3 sections, structured not markdown)
  3. leader outputs DelegationDecision (Pydantic model, not @mention)
  4. DelegationDecision creates child tasks for chosen member(s)
  5. leader task completes → STOP
  6. member task completes → event loop checks: all members done?
     yes → re-enqueue leader task for evaluation
     no  → wait for next member completion
```

### Why structured delegation, not @mention

| Aspect | multica @mention | ariadne DelegationDecision |
|--------|-----------------|-------------------------------|
| Format | `[@Name](mention://agent/<UUID>)` markdown | `DelegationDecision(target_agent, backend, reason)` |
| Testability | Cannot unit-test markdown parsing reliably | Pydantic model, directly assertable |
| Replayability | Must replay prompt + parse markdown | Serialize decision, replay from JSON |
| Error mode | Leader typos mention → task never delivered | Validation rejects unknown agent_id |

This is a deliberate, documented deviation. See multica-mapping.md.

## DelegationDecision Model

```python
class DelegationDecision(BaseModel):
    """Output of the leader agent's delegation reasoning."""
    target_agent_id: str       # must be a squad member
    backend: str               # "codex" | "claude-code"
    handoff_prompt: str        # what to tell the member
    reason: str                # why this agent + backend
    skill_refs: list[str]      # skills to attach to member task
```

## Briefing Structure

> Mirrors multica's `buildSquadLeaderBriefing` output, structured for Python

```python
class SquadBriefing(BaseModel):
    protocol: str              # Operating Protocol (constant, see below)
    roster: list[RosterEntry]  # Members with capabilities
    instructions: str          # User-defined squad instructions

class RosterEntry(BaseModel):
    agent_id: str
    name: str
    role: str                  # e.g. "coder", "reviewer"
    skills: list[str]          # skill names
    backends: list[str]        # available execution backends
```

### Operating Protocol (adapted from multica, English)

```
You are a Squad LEADER. Your job is to COORDINATE, not to implement.

1. Read the issue and decide which member is best suited.
   Match the task to each member's skills and role.
2. Output a DelegationDecision naming the chosen member, the backend
   to use, and a handoff prompt. Do NOT do the work yourself.
3. After all delegated members complete, you will be re-activated.
   Read their results and decide: delegate next step, or mark done.
4. If no member can handle the task, set handoff_prompt to explain
   the gap instead of doing the work.

Hard rules:
- Do NOT implement. Doing it yourself defeats the squad.
- Do NOT delegate to members not in the roster.
- One delegation per activation. Do not spam multiple decisions.
```

## Event Loop

```python
# orchestrator.py (pseudocode)

def on_member_task_complete(task: Task):
    """Called when a member task reaches terminal state."""
    squad_id = task.squad_id
    pending = store.count_pending_member_tasks(squad_id)
    if pending == 0:
        # All members done → re-activate leader for evaluation
        leader = store.get_squad_leader(squad_id)
        store.enqueue_task(
            issue_id=task.issue_id,
            agent_id=leader.id,
            squad_id=squad_id,
            kind="leader_evaluation",
        )
    # else: wait for remaining members
```

## LangGraph Integration

The leader's single activation is modeled as a LangGraph supervisor graph:

```
        ┌───────────┐
        │   START    │
        └─────┬──────┘
              ▼
        ┌───────────┐
        │  leader   │  read briefing + issue → output DelegationDecision
        │  (node)   │
        └─────┬─────┘
              │ conditional edge: has delegation?
         ┌────┴────┐
         ▼         ▼
    ┌─────────┐  ┌──────┐
    │ delegate │  │ done │  (no more work, mark issue done)
    │ (node)   │  └──────┘
    └─────┬───┘
          │ creates child task(s)
          ▼
      ┌────────┐
      │  END   │
      └────────┘
```

The graph runs **once per leader activation**, not once per issue lifecycle.
The event loop (outside LangGraph) controls when to re-invoke the graph.

## Squad Model

> Derives from: multica migration 084

```python
class Squad(BaseModel):
    id: str
    name: str
    leader_id: str           # must be an agent
    instructions: str = ""

class SquadMember(BaseModel):
    squad_id: str
    member_type: str         # "agent" (humans not supported in v1)
    member_id: str           # agent_id
    role: str                # "coder", "reviewer", "tester"
```

## Tests Required

| Test | What it verifies |
|------|-----------------|
| `test_leader_only_delegates` | Leader task output is a DelegationDecision, never an execution result |
| `test_delegation_creates_child_tasks` | DelegationDecision → child task created for target_agent |
| `test_event_loop_re_activates_leader` | All members complete → new leader task enqueued |
| `test_event_loop_waits_for_pending` | Some members pending → no new leader task |
| `test_briefing_contains_roster` | Briefing includes all non-archived members with skills + backends |
| `test_delegation_rejects_unknown_agent` | DelegationDecision with agent_id not in roster → error |
| `test_leader_evaluation_marks_done` | Second leader activation with no work → issue marked done |
