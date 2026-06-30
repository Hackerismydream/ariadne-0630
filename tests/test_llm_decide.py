"""Tests for llm_decide.py — LLM-backed delegation decision.

Per docs/plan/tasks/squad-003.md "test_llm_decide.py must cover".
"""

import json

import pytest

from ariadne.briefing import generate_briefing
from ariadne.llm_decide import _parse_response, make_llm_decide
from ariadne.models import AssigneeType, SquadBriefing
from ariadne.orchestrator import deterministic_decide
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def briefing_and_issue(store):
    leader = store.create_agent("Leader", "", ["dry-run"], [])
    member = store.create_agent("Coder", "", ["codex"], ["python"])
    squad = store.create_squad("S", leader.id)
    store.add_squad_member(squad.id, member.id, role="coder")
    briefing = generate_briefing(store, squad.id)
    issue = store.create_issue("Build X", "Do the thing", AssigneeType.SQUAD, squad.id)
    return briefing, issue, member


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def test_parse_valid_delegation():
    """fake LLM returns valid JSON → DelegationDecision created"""
    text = json.dumps({
        "target_agent_id": "agent-123",
        "backend": "codex",
        "handoff_prompt": "implement the feature",
        "reason": "best python skill",
        "skill_refs": ["python"],
    })
    briefing = SquadBriefing(protocol="p", roster=[], instructions="")
    decision = _parse_response(text, briefing)
    assert decision is not None
    assert decision.target_agent_id == "agent-123"
    assert decision.backend == "codex"
    assert decision.skill_refs == ["python"]


def test_parse_none_delegation():
    """fake LLM returns {"delegation": "none"} → None"""
    text = json.dumps({"delegation": "none"})
    briefing = SquadBriefing(protocol="p", roster=[], instructions="")
    assert _parse_response(text, briefing) is None


def test_parse_json_in_markdown_codeblock():
    """LLM wraps JSON in ```json ... ``` — should still parse"""
    text = '```json\n{"target_agent_id": "a1", "backend": "codex", "handoff_prompt": "do it", "reason": "ok", "skill_refs": []}\n```'
    briefing = SquadBriefing(protocol="p", roster=[], instructions="")
    decision = _parse_response(text, briefing)
    assert decision is not None
    assert decision.target_agent_id == "a1"


def test_parse_garbage_returns_none():
    """unparseable text → None (no crash)"""
    briefing = SquadBriefing(protocol="p", roster=[], instructions="")
    assert _parse_response("this is not json at all", briefing) is None


def test_parse_missing_field_returns_none():
    """JSON missing required field → None"""
    text = json.dumps({"target_agent_id": "a1", "backend": "codex"})
    briefing = SquadBriefing(protocol="p", roster=[], instructions="")
    assert _parse_response(text, briefing) is None


# ---------------------------------------------------------------------------
# make_llm_decide — fallback behavior
# ---------------------------------------------------------------------------


def test_fallback_without_key(briefing_and_issue, monkeypatch):
    """api_key=None and no env var → uses deterministic_decide"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    briefing, issue, member = briefing_and_issue

    decide = make_llm_decide(api_key=None)
    # Should be the same function object as deterministic_decide
    assert decide is deterministic_decide

    result = decide(briefing, issue)
    assert result is not None
    assert result.target_agent_id == member.id


def test_graceful_on_api_error(briefing_and_issue, monkeypatch):
    """API raises → returns deterministic fallback (no crash)"""
    briefing, issue, member = briefing_and_issue

    # Set a fake key so it doesn't fall back at construction time
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-testing")

    decide = make_llm_decide(api_key="fake-key")

    # Mock the OpenAI import to raise — simulates API/connection error.
    # The decide function catches all exceptions and falls back to deterministic.
    import builtins
    real_import = builtins.__import__

    def fail_openai(name, *args, **kwargs):
        if name == "openai":
            raise RuntimeError("connection refused")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_openai)

    result = decide(briefing, issue)

    # Should fall back to deterministic, not crash
    assert result is not None
    assert result.target_agent_id == member.id
