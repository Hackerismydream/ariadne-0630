"""Tests for real squad loop: multi-step delegation, LLM fallback, trace propagation.

Per docs/plan/tasks/deep-003.md. Uses mock backends, not real CLI.
"""

import pytest

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType, DelegationDecision, IssueStatus
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def squad_with_two_members(store):
    """Squad with leader + coder + tester."""
    leader = store.create_agent("Leader", "", ["dry-run"], ["planning"])
    coder = store.create_agent("Coder", "", ["dry-run"], ["python"])
    tester = store.create_agent("Tester", "", ["dry-run"], ["testing"])

    squad = store.create_squad("Dev", leader.id, instructions="build it")
    store.add_squad_member(squad.id, coder.id, role="coder")
    store.add_squad_member(squad.id, tester.id, role="tester")

    issue = store.create_issue("Build feature", "Do the thing", AssigneeType.SQUAD, squad.id)
    store.enqueue_task(issue.id, leader.id, squad_id=squad.id)

    return squad, leader, coder, tester, issue


# ---------------------------------------------------------------------------
# Multi-step delegation
# ---------------------------------------------------------------------------


def test_squad_multi_step_delegation(store, squad_with_two_members):
    """Leader delegates to first member, then second, then marks done."""
    squad, leader, coder, tester, issue = squad_with_two_members
    call_count = [0]

    def multi_step_decide(briefing, issue, completed_results=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # First: delegate to coder
            entry = next(e for e in briefing.roster if e.name == "Coder")
            return DelegationDecision(
                target_agent_id=entry.agent_id, backend="dry-run",
                handoff_prompt="implement feature", reason="coder first",
                skill_refs=[],
            )
        elif call_count[0] == 2:
            # Second: delegate to tester
            entry = next(e for e in briefing.roster if e.name == "Tester")
            return DelegationDecision(
                target_agent_id=entry.agent_id, backend="dry-run",
                handoff_prompt="write tests", reason="tester second",
                skill_refs=[],
            )
        else:
            # Third: done
            return None

    orc = Orchestrator(store=store, llm_decide=multi_step_decide)
    daemon = Daemon(
        store=store, backend_factory=get_backend,
        poll_interval=0.001, orchestrator=orc,
    )
    daemon.start(max_iterations=15)

    # Issue should be done
    assert store.get_issue(issue.id).status == IssueStatus.DONE
    # Leader activated 3 times
    assert call_count[0] >= 3

    # Both coder and tester should have completed tasks
    coder_tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND status = 'completed'", (coder.id,)
    ).fetchall()
    assert len(coder_tasks) >= 1

    tester_tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND status = 'completed'", (tester.id,)
    ).fetchall()
    assert len(tester_tasks) >= 1


# ---------------------------------------------------------------------------
# LLM failure fallback
# ---------------------------------------------------------------------------


def test_llm_failure_fallback_to_deterministic(store, squad_with_two_members):
    """LLM returns garbage 3 times → falls back to deterministic."""
    squad, leader, coder, tester, issue = squad_with_two_members

    garbage_count = [0]

    def garbage_decide(briefing, issue, completed_results=None):
        garbage_count[0] += 1
        if garbage_count[0] <= 3:
            return "garbage"  # Not a DelegationDecision, will cause issues
        # After 3 garbage, the orchestrator should have fallen back
        return None

    # The orchestrator's llm_decide is called directly, not through make_llm_decide.
    # The 3-failure fallback is in make_llm_decide, not in the orchestrator.
    # For this test, we test that make_llm_decide falls back after 3 failures.
    from ariadne.llm_decide import make_llm_decide

    # Set a fake key so it doesn't fall back at construction
    import os
    os.environ["DEEPSEEK_API_KEY"] = "fake-key-test"

    decide = make_llm_decide(api_key="fake-key-test")

    # Mock the openai import to always fail
    import builtins
    real_import = builtins.__import__

    def fail_openai(name, *args, **kwargs):
        if name == "openai":
            raise RuntimeError("simulated API failure")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fail_openai

    from ariadne.briefing import generate_briefing
    briefing = generate_briefing(store, squad.id)

    # Call decide 3 times — each should fail and fall back
    decide(briefing, issue)
    decide(briefing, issue)
    decide(briefing, issue)

    # All should return deterministic results (not crash)
    # First call should not crash
    # After 3 failures, subsequent calls should also fall back
    result = decide(briefing, issue)
    assert result is not None  # deterministic picks first member

    # Restore
    builtins.__import__ = real_import
    del os.environ["DEEPSEEK_API_KEY"]


# ---------------------------------------------------------------------------
# Trace propagation
# ---------------------------------------------------------------------------


def test_squad_trace_propagation(store, squad_with_two_members):
    """All tasks in the squad loop share the same trace_id."""
    squad, leader, coder, tester, issue = squad_with_two_members

    call_count = [0]
    def simple_decide(briefing, issue, completed_results=None):
        call_count[0] += 1
        if call_count[0] == 1:
            entry = briefing.roster[0]
            return DelegationDecision(
                target_agent_id=entry.agent_id, backend="dry-run",
                handoff_prompt="do it", reason="first", skill_refs=[],
            )
        return None

    orc = Orchestrator(store=store, llm_decide=simple_decide)
    daemon = Daemon(
        store=store, backend_factory=get_backend,
        poll_interval=0.001, orchestrator=orc,
    )
    daemon.start(max_iterations=10)

    # All tasks should share the same trace_id
    tasks = store._conn.execute(
        "SELECT DISTINCT trace_id FROM task WHERE issue_id = ?", (issue.id,)
    ).fetchall()
    assert len(tasks) == 1  # all same trace_id


# ---------------------------------------------------------------------------
# Member results in re-evaluation
# ---------------------------------------------------------------------------


def test_member_results_in_re_evaluation(store, squad_with_two_members):
    """Leader receives completed member results on re-evaluation."""
    squad, leader, coder, tester, issue = squad_with_two_members

    received_results = [None]

    def tracking_decide(briefing, issue, completed_results=None):
        received_results[0] = completed_results
        if completed_results and len(completed_results) > 0:
            return None  # done after seeing results
        entry = briefing.roster[0]
        return DelegationDecision(
            target_agent_id=entry.agent_id, backend="dry-run",
            handoff_prompt="do it", reason="first", skill_refs=[],
        )

    orc = Orchestrator(store=store, llm_decide=tracking_decide)
    daemon = Daemon(
        store=store, backend_factory=get_backend,
        poll_interval=0.001, orchestrator=orc,
    )
    daemon.start(max_iterations=10)

    # On second activation, leader should have received completed results
    assert received_results[0] is not None
    assert len(received_results[0]) >= 1
    assert received_results[0][0]["status"] == "completed"
