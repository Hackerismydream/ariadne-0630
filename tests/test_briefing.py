"""Tests for briefing.py — Squad leader briefing generation.

Per docs/plan/tasks/squad-001.md "test_briefing.py must cover".
"""

import pytest

from ariadne.briefing import OPERATING_PROTOCOL, generate_briefing
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def squad_with_members(store):
    """Create a squad with a leader + 2 members. Returns (squad, leader, member1, member2)."""
    leader = store.create_agent("Leader", "coordinate", ["dry-run"], ["planning"])
    member1 = store.create_agent("Coder", "write code", ["codex"], ["python", "testing"])
    member2 = store.create_agent("Reviewer", "review code", ["claude-code"], ["code-review"])

    squad = store.create_squad("Alpha", leader.id, instructions="Ship the feature")
    store.add_squad_member(squad.id, member1.id, role="coder")
    store.add_squad_member(squad.id, member2.id, role="reviewer")

    return squad, leader, member1, member2


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_briefing_has_three_sections(store, squad_with_members):
    """protocol, roster, instructions all populated"""
    squad, leader, _, _ = squad_with_members
    briefing = generate_briefing(store, squad.id)

    assert briefing.protocol != ""
    assert briefing.protocol == OPERATING_PROTOCOL
    assert len(briefing.roster) > 0
    assert briefing.instructions == "Ship the feature"


# ---------------------------------------------------------------------------
# Roster membership
# ---------------------------------------------------------------------------


def test_roster_contains_all_members(store, squad_with_members):
    """2 members → roster has 2 entries"""
    squad, leader, m1, m2 = squad_with_members
    briefing = generate_briefing(store, squad.id)

    assert len(briefing.roster) == 2
    agent_ids = {r.agent_id for r in briefing.roster}
    assert agent_ids == {m1.id, m2.id}


def test_roster_excludes_leader(store, squad_with_members):
    """leader agent is not in roster"""
    squad, leader, m1, m2 = squad_with_members
    briefing = generate_briefing(store, squad.id)

    roster_ids = {r.agent_id for r in briefing.roster}
    assert leader.id not in roster_ids


def test_roster_entry_has_skills_and_backends(store, squad_with_members):
    """each entry has correct skills/backends from agent"""
    squad, leader, m1, m2 = squad_with_members
    briefing = generate_briefing(store, squad.id)

    coder_entry = next(r for r in briefing.roster if r.agent_id == m1.id)
    assert coder_entry.name == "Coder"
    assert coder_entry.role == "coder"
    assert coder_entry.skills == ["python", "testing"]
    assert coder_entry.backends == ["codex"]

    reviewer_entry = next(r for r in briefing.roster if r.agent_id == m2.id)
    assert reviewer_entry.name == "Reviewer"
    assert reviewer_entry.role == "reviewer"
    assert reviewer_entry.skills == ["code-review"]
    assert reviewer_entry.backends == ["claude-code"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_squad_roster(store):
    """squad with 0 members → roster is empty list, no error"""
    leader = store.create_agent("Solo", "", ["dry-run"], [])
    squad = store.create_squad("Solo", leader.id)

    briefing = generate_briefing(store, squad.id)
    assert briefing.roster == []
    assert briefing.protocol == OPERATING_PROTOCOL


def test_instructions_from_squad(store, squad_with_members):
    """squad.instructions appears in briefing.instructions"""
    squad, _, _, _ = squad_with_members
    briefing = generate_briefing(store, squad.id)
    assert briefing.instructions == "Ship the feature"


def test_protocol_contains_key_rules(store, squad_with_members):
    """protocol text mentions key rules"""
    squad, _, _, _ = squad_with_members
    briefing = generate_briefing(store, squad.id)

    assert "COORDINATE" in briefing.protocol
    assert "DelegationDecision" in briefing.protocol
    assert "not in the roster" in briefing.protocol


def test_missing_agent_skipped(store):
    """squad member whose agent was deleted → skipped silently, no crash"""
    leader = store.create_agent("Leader", "", ["dry-run"], [])
    member = store.create_agent("Member", "", ["codex"], ["python"])
    squad = store.create_squad("S", leader.id)
    store.add_squad_member(squad.id, member.id, role="coder")

    # Simulate deleted agent by removing it directly from DB
    store._conn.execute("DELETE FROM agent WHERE id = ?", (member.id,))
    store._conn.commit()

    briefing = generate_briefing(store, squad.id)
    assert len(briefing.roster) == 0
