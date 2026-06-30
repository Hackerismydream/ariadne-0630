"""End-to-end test: full Squad delegation loop.

issue → leader claim → delegation → member claim → execute (dry-run)
→ event loop → leader re-evaluation → issue done

Per docs/plan/tasks/squad-003.md "test_squad_e2e.py must cover".
"""

import pytest

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType, IssueStatus, TaskStatus
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_squad_full_loop(store):
    """Full loop: leader → delegate → member execute → leader re-eval → done.

    Uses deterministic_decide (no LLM needed).
    The deterministic_decide always delegates on first activation (picks first member),
    and on second activation (re-evaluation after member completes) it will try to
    delegate again — but since there's only one member and the issue is not yet done,
    it delegates again. To make the loop terminate, we use a custom decider that
    delegates once then returns None.
    """
    # Setup: leader + 1 member squad
    leader = store.create_agent("Leader", "coordinate", ["dry-run"], [])
    member = store.create_agent("Coder", "write code", ["dry-run"], ["python"])
    squad = store.create_squad("Alpha", leader.id, instructions="build it")
    store.add_squad_member(squad.id, member.id, role="coder")

    issue = store.create_issue("Build feature X", "Implement the thing", AssigneeType.SQUAD, squad.id)

    # Enqueue the initial leader task
    store.enqueue_task(issue.id, leader.id, squad_id=squad.id)

    # Custom decider: delegate on first call, return None on second (re-evaluation)
    call_count = [0]

    def counting_decide(briefing, issue):
        call_count[0] += 1
        if call_count[0] == 1:
            # First activation: delegate to the member
            from ariadne.models import DelegationDecision
            entry = briefing.roster[0]
            return DelegationDecision(
                target_agent_id=entry.agent_id,
                backend=entry.backends[0] if entry.backends else "dry-run",
                handoff_prompt=f"Work on: {issue.title}",
                reason="first delegation",
                skill_refs=entry.skills,
            )
        else:
            # Second activation: no more work
            return None

    orc = Orchestrator(store=store, llm_decide=counting_decide)
    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="test-rt",
        poll_interval=0.001,
        orchestrator=orc,
    )

    # Run enough iterations to complete the full loop:
    # iter 1: leader claims + delegates (creates member task)
    # iter 2: member claims + executes (dry-run) + event loop (re-enqueues leader)
    # iter 3: leader claims + re-evaluates (returns None → issue done)
    daemon.start(max_iterations=10)

    # Verify: issue should be done
    final_issue = store.get_issue(issue.id)
    assert final_issue.status == IssueStatus.DONE

    # Verify: leader was activated at least twice
    assert call_count[0] >= 2

    # Verify: member task was executed (completed)
    member_tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ? AND squad_id = ?",
        (member.id, squad.id),
    ).fetchall()
    assert len(member_tasks) >= 1
    # At least one should be completed
    statuses = [t["status"] for t in member_tasks]
    assert "completed" in statuses

    # Verify: all leader tasks are completed
    leader_tasks = store._conn.execute(
        "SELECT * FROM task WHERE agent_id = ?",
        (leader.id,),
    ).fetchall()
    for t in leader_tasks:
        assert t["status"] in ("completed", "queued"), f"unexpected leader task status: {t['status']}"
