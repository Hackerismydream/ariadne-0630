id: squad-001
scope: squad
status: done
depends-on: [core-003]
```

## Objective

Implement `src/ariadne/briefing.py`: generate the 3-section SquadBriefing
(protocol + roster + instructions) that gets injected into a leader task.
This is the leader's "world view" — it tells the leader who its members are,
what they can do, and the rules of delegation.

## Context

- Design doc: [docs/architecture/squad-orchestration.md](../../architecture/squad-orchestration.md) — briefing structure, Operating Protocol, RosterEntry
- Multica mapping: [docs/architecture/multica-mapping.md](../../architecture/multica-mapping.md) — mechanism 2
- Doc index: [docs/INDEX.md](../../INDEX.md)

## Path

```
src/ariadne/briefing.py
tests/test_briefing.py
```

## Requirements

### briefing.py

```python
def generate_briefing(store: Store, squad_id: str) -> SquadBriefing:
    """Build the leader briefing from squad + members + agents.

    1. Load squad + members from store.
    2. For each member, load agent → build RosterEntry (name, role, skills, backends).
    3. Assemble SquadBriefing with the Operating Protocol constant.
    """
```

### Operating Protocol (constant, adapted from multica)

The protocol text from squad-orchestration.md "Operating Protocol" section.
Store as a module-level constant `OPERATING_PROTOCOL`.

Key rules that must be in the text:
- You are a Squad LEADER. Your job is to COORDINATE, not to implement.
- Read the issue, decide which member is best suited.
- Output a DelegationDecision — do NOT do the work yourself.
- After all members complete, you will be re-activated.
- If no member can handle the task, explain the gap.
- Do NOT delegate to members not in the roster.
- One delegation per activation.

### RosterEntry building

For each squad member:
- Load the agent via `store.get_agent(member.member_id)`
- Skip if agent is None (deleted agent race)
- Build RosterEntry with: agent_id, name, role (from squad_member), skills (from agent), backends (from agent)

### Constraints

- briefing.py imports only: store, models
- No LLM calls — this is pure data assembly
- If squad has no members, roster is empty list (not an error)
- The leader itself should NOT appear in the roster (prevent self-delegation)

## Verification

```bash
ruff check src/ariadne/briefing.py
pytest tests/test_briefing.py -v
```

### test_briefing.py must cover:

- `test_briefing_has_three_sections`: protocol, roster, instructions all populated
- `test_roster_contains_all_members`: 2 members → roster has 2 entries
- `test_roster_excludes_leader`: leader agent is not in roster
- `test_roster_entry_has_skills_and_backends`: each entry has correct skills/backends from agent
- `test_empty_squad_roster`: squad with 0 members → roster is empty list, no error
- `test_instructions_from_squad`: squad.instructions appears in briefing.instructions
- `test_protocol_contains_key_rules`: protocol text mentions "COORDINATE", "DelegationDecision", "not in the roster"
- `test_missing_agent_skipped`: squad member whose agent was deleted → skipped silently, no crash
