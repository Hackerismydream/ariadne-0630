"""Squad orchestrator: leader delegation + event loop.

Replaces Ariadne's 1779-line waterfall with multica-style event-driven
orchestration: leader claims → reads briefing → outputs DelegationDecision →
child task created → leader stops → member completes → event loop
re-activates leader.

Per docs/architecture/squad-orchestration.md.
"""

from __future__ import annotations

import logging
from typing import Callable

from ariadne.briefing import generate_briefing
from ariadne.models import DelegationDecision, FailureReason, Issue, IssueStatus, SquadBriefing, Task, TaskStatus
from ariadne.store import Store

logger = logging.getLogger(__name__)


def deterministic_decide(
    briefing: SquadBriefing, issue: Issue
) -> DelegationDecision | None:
    """Simple delegation: pick the first member with a matching backend.

    This is the fallback when no LLM is available. It picks the first
    roster entry and uses its first backend.
    """
    if not briefing.roster:
        return None

    member = briefing.roster[0]
    backend = member.backends[0] if member.backends else "dry-run"

    return DelegationDecision(
        target_agent_id=member.agent_id,
        backend=backend,
        handoff_prompt=f"Work on issue: {issue.title}\n\n{issue.description}",
        reason=f"Selected {member.name} ({member.role}) as the first available member",
        skill_refs=member.skills,
    )


class Orchestrator:
    """Squad leader delegation + event loop."""

    def __init__(
        self,
        store: Store,
        llm_decide: Callable[[SquadBriefing, Issue], DelegationDecision | None] | None = None,
    ):
        self.store = store
        self.llm_decide = llm_decide or deterministic_decide

    def handle_leader_task(self, task: Task) -> None:
        """Process one leader activation.

        1. Load squad + generate briefing
        2. Call llm_decide → DelegationDecision or None
        3. If DelegationDecision: validate, create child task
        4. If None: mark issue as done
        5. Mark leader task completed
        """
        # Reload from store to get the latest status (may have been claimed by daemon)
        task = self.store.get_task(task.id)
        if task is None:
            logger.error("task not found")
            return

        if task.squad_id is None:
            logger.error("task %s has no squad_id — not a leader task", task.id)
            return

        squad = self.store.get_squad(task.squad_id)
        if squad is None:
            logger.error("squad %s not found for task %s", task.squad_id, task.id)
            self.store.fail_task(task.id, "squad not found", FailureReason.AGENT_ERROR)
            return

        # Leader must be the squad's leader agent
        if task.agent_id != squad.leader_id:
            logger.warning("task %s agent %s is not the squad leader %s", task.id, task.agent_id, squad.leader_id)

        briefing = generate_briefing(self.store, task.squad_id)
        issue = self.store.get_issue(task.issue_id)
        if issue is None:
            logger.error("issue %s not found for task %s", task.issue_id, task.id)
            self.store.fail_task(task.id, "issue not found", FailureReason.AGENT_ERROR)
            return

        decision = self.llm_decide(briefing, issue)

        if decision is None:
            # Leader decided no more work needed → mark issue done
            logger.info("leader decided no delegation — marking issue %s done", issue.id)
            self.store.update_issue_status(issue.id, IssueStatus.DONE)
        else:
            # Validate delegation
            error = self._validate_delegation(decision, briefing)
            if error:
                logger.error("delegation validation failed: %s", error)
                if task.status == TaskStatus.CLAIMED:
                    self.store.start_task(task.id)
                self.store.fail_task(task.id, error, FailureReason.AGENT_ERROR)
                return

            # Create child task for the member — inherit leader's trace_id
            child = self.store.enqueue_task(
                issue_id=issue.id,
                agent_id=decision.target_agent_id,
                squad_id=task.squad_id,
                handoff_prompt=decision.handoff_prompt,
                trace_id=task.trace_id,
            )
            self.store.log_activity(
                task.trace_id, child.id, "delegated",
                {"to_agent": decision.target_agent_id, "backend": decision.backend},
            )
            logger.info(
                "leader delegated to agent %s via backend %s, child task %s (trace=%s)",
                decision.target_agent_id,
                decision.backend,
                child.id,
                task.trace_id,
            )

        # Mark leader task completed (task should already be running —
        # the daemon claims+starts before calling handle_leader_task.
        # Only start if still in claimed state.)
        if task.status == TaskStatus.CLAIMED:
            self.store.start_task(task.id)
        self.store.complete_task(task.id, {"delegation": decision.model_dump() if decision else None})

    def on_member_task_complete(self, task: Task) -> None:
        """Event loop callback when a member task reaches terminal state.

        1. Check if any pending member tasks remain for this squad
        2. If none remain: re-enqueue leader task for evaluation
        3. If some remain: do nothing (wait)
        """
        if task.squad_id is None:
            return

        pending = self.store.get_pending_member_tasks(task.squad_id)
        # Exclude leader tasks from the pending count — only member tasks matter
        squad = self.store.get_squad(task.squad_id)
        if squad:
            pending = [t for t in pending if t.agent_id != squad.leader_id]
        if not pending:
            # All members done → re-activate leader
            squad = self.store.get_squad(task.squad_id)
            if squad is None:
                logger.error("squad %s not found during event loop", task.squad_id)
                return

            issue = self.store.get_issue(task.issue_id)
            if issue is None:
                return
            if issue.status == IssueStatus.DONE:
                # Issue already done — no need to re-activate leader
                return

            leader_task = self.store.enqueue_task(
                issue_id=task.issue_id,
                agent_id=squad.leader_id,
                squad_id=task.squad_id,
            )
            logger.info(
                "event loop: all members done, re-activated leader task %s for issue %s",
                leader_task.id,
                task.issue_id,
            )
        else:
            logger.info(
                "event loop: %d pending member tasks for squad %s, waiting",
                len(pending),
                task.squad_id,
            )

    def _validate_delegation(
        self, decision: DelegationDecision, briefing: SquadBriefing
    ) -> str | None:
        """Validate a DelegationDecision against the roster.

        Returns an error message string if invalid, None if valid.
        """
        roster_ids = {entry.agent_id for entry in briefing.roster}
        if decision.target_agent_id not in roster_ids:
            return f"target_agent_id '{decision.target_agent_id}' not in roster"

        # Validate backend name
        try:
            from ariadne.backends import get_backend
            get_backend(decision.backend)
        except ValueError:
            return f"unknown backend '{decision.backend}'"

        return None
