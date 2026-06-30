"""Squad leader briefing generation.

Builds the 3-section briefing (protocol + roster + instructions) injected
into a leader task. Mirrors multica's buildSquadLeaderBriefing, structured
for Python rather than markdown string concatenation.

Per docs/architecture/squad-orchestration.md.
"""

from __future__ import annotations

from ariadne.models import RosterEntry, SquadBriefing
from ariadne.store import Store

# ---------------------------------------------------------------------------
# Operating Protocol (adapted from multica squadOperatingProtocol)
# ---------------------------------------------------------------------------

OPERATING_PROTOCOL = """## Squad Operating Protocol

You are a Squad LEADER. Your job is to COORDINATE, not to implement.

Your responsibilities, in order:

1. Read the issue (title, description) and decide which member is best
   suited to do the work. Match the task to each member's listed skills
   and role in the Squad Roster below.

2. Output a DelegationDecision naming the chosen member, the backend to
   use, and a handoff prompt. Do NOT do the work yourself — doing it
   yourself defeats the entire purpose of the squad.

3. After all delegated members complete, you will be re-activated.
   Read their results and decide: delegate the next step, or mark done.

4. If no member can handle the task, explain the gap instead of doing
   the work.

Hard rules:
- Do NOT implement. Doing it yourself is a protocol violation.
- Do NOT delegate to members not in the roster.
- One delegation per activation. Do not spam multiple decisions.
- If no delegation is needed, return no decision to mark the issue done.
"""


def generate_briefing(store: Store, squad_id: str) -> SquadBriefing:
    """Build the leader briefing from squad + members + agents.

    1. Load squad + members from store.
    2. For each member, load agent → build RosterEntry.
    3. Assemble SquadBriefing with the Operating Protocol constant.

    The leader itself is excluded from the roster to prevent self-delegation.
    Members whose agent can't be loaded are silently skipped.
    """
    squad = store.get_squad(squad_id)
    if squad is None:
        raise KeyError(f"squad not found: {squad_id}")

    members = store.get_squad_members(squad_id)

    roster: list[RosterEntry] = []
    for member in members:
        # Skip the leader if they appear in the member list — prevents self-delegation
        if member.member_id == squad.leader_id:
            continue

        agent = store.get_agent(member.member_id)
        if agent is None:
            # Deleted agent race — skip silently
            continue

        roster.append(
            RosterEntry(
                agent_id=agent.id,
                name=agent.name,
                role=member.role,
                skills=agent.skills,
                backends=agent.backends,
            )
        )

    return SquadBriefing(
        protocol=OPERATING_PROTOCOL,
        roster=roster,
        instructions=squad.instructions,
    )
