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
from ariadne.models import (
    DelegationDecision,
    FailureReason,
    Issue,
    IssueStatus,
    LeaderDecision,
    LeaderDecisionOutcome,
    SquadBriefing,
    Task,
    TaskStatus,
)
from ariadne.store import Store

logger = logging.getLogger(__name__)


def deterministic_decide(
    briefing: SquadBriefing, issue: Issue, completed_results: list[dict] | None = None
) -> DelegationDecision | None:
    """Simple delegation: pick the first member with a matching backend.

    This is the fallback when no LLM is available. It picks the first
    roster entry and uses its first backend. If completed_results show
    members have already worked, returns None (marks done).
    """
    if completed_results:
        # If any member has completed, deterministic says "done"
        if any(r["status"] == "completed" for r in completed_results):
            return None

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
        llm_decide: Callable[
            [SquadBriefing, Issue], DelegationDecision | LeaderDecision | None
        ]
        | None = None,
    ):
        self.store = store
        self.llm_decide = llm_decide or deterministic_decide

    def handle_leader_task(self, task: Task) -> None:
        """Process one leader activation.

        1. Load squad + generate briefing
        2. Call llm_decide → DelegationDecision, LeaderDecision, or legacy None
        3. Record a LeaderDecision fact for action/no_action/failed/done
        4. If action: validate, create child TaskRun
        5. If done: close the Issue
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

        # Gather completed member results for re-evaluation context
        completed_results = self._gather_completed_results(task.squad_id, issue.id)

        raw_decision = self.llm_decide(briefing, issue, completed_results)
        decision = self._coerce_leader_decision(
            raw_decision, completed_results=completed_results
        )

        if decision.outcome == LeaderDecisionOutcome.ACTION:
            delegation = self._delegation_from_leader_decision(decision)
            if delegation is None:
                self._record_failed_turn(
                    task,
                    issue,
                    "action decision missing delegation payload",
                )
                return

            error = self._validate_delegation(delegation, briefing)
            if error:
                logger.error("delegation validation failed: %s", error)
                self._record_failed_turn(task, issue, error)
                return

            child = self.store.enqueue_taskrun(
                issue_id=issue.id,
                agent_profile_id=delegation.target_agent_id,
                squad_id=task.squad_id,
                handoff_prompt=delegation.handoff_prompt,
                trace_id=task.trace_id,
            )
            record = self.store.record_leader_decision(
                issue_id=issue.id,
                squad_id=task.squad_id,
                leader_task_id=task.id,
                outcome=LeaderDecisionOutcome.ACTION,
                reason=decision.reason or delegation.reason,
                delegation_payload=delegation.model_dump(),
                created_taskrun_ids=[child.id],
            )
            if task.trace_id:
                self.store.log_activity(
                    task.trace_id,
                    child.id,
                    "delegated",
                    {"to_agent": delegation.target_agent_id, "backend": delegation.backend},
                )
            logger.info(
                "leader delegated to agent %s via backend %s, child task %s (trace=%s)",
                delegation.target_agent_id,
                delegation.backend,
                child.id,
                task.trace_id,
            )
            self._complete_leader_task(task.id, record)
            return

        if decision.outcome == LeaderDecisionOutcome.NO_ACTION:
            record = self.store.record_leader_decision(
                issue_id=issue.id,
                squad_id=task.squad_id,
                leader_task_id=task.id,
                outcome=LeaderDecisionOutcome.NO_ACTION,
                reason=decision.reason,
                delegation_payload=decision.delegation_payload,
                created_taskrun_ids=[],
            )
            self._complete_leader_task(task.id, record)
            return

        if decision.outcome == LeaderDecisionOutcome.FAILED:
            self._record_failed_turn(
                task,
                issue,
                decision.reason or "leader coordination failed",
                delegation_payload=decision.delegation_payload,
                mark_issue_failed=self._all_completed_results_failed(
                    completed_results
                ),
            )
            return

        if decision.outcome == LeaderDecisionOutcome.DONE:
            payload = dict(decision.delegation_payload)
            payload.setdefault(
                "issue_timeline_event_count",
                len(self.store.get_issue_timeline(issue.id)),
            )
            record = self.store.record_leader_decision(
                issue_id=issue.id,
                squad_id=task.squad_id,
                leader_task_id=task.id,
                outcome=LeaderDecisionOutcome.DONE,
                reason=decision.reason,
                delegation_payload=payload,
                created_taskrun_ids=[],
            )
            self.store.update_issue_status(issue.id, IssueStatus.DONE)
            self._complete_leader_task(task.id, record)
            return

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
            if issue.status in (
                IssueStatus.DONE,
                IssueStatus.FAILED,
                IssueStatus.CANCELLED,
            ):
                # Issue already terminal — no need to re-activate leader
                return

            leader_task = self.store.enqueue_taskrun(
                issue_id=task.issue_id,
                agent_profile_id=squad.leader_id,
                squad_id=task.squad_id,
                trace_id=task.trace_id,
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

    def _gather_completed_results(self, squad_id: str, issue_id: str) -> list[dict]:
        """Gather results of completed/failed member tasks for leader re-evaluation."""
        rows = self.store._conn.execute(
            """SELECT t.id, t.agent_id, t.status, t.result, t.error, a.name as agent_name
               FROM task t
               JOIN squad s ON s.id = t.squad_id
               LEFT JOIN agent a ON t.agent_id = a.id
               WHERE t.squad_id = ? AND t.issue_id = ?
                 AND t.status IN ('completed', 'failed')
                 AND t.agent_id != s.leader_id
               ORDER BY t.created_at""",
            (squad_id, issue_id),
        ).fetchall()
        import json as _json
        results = []
        for r in rows:
            results.append({
                "task_id": r["id"],
                "agent_name": r["agent_name"] or "unknown",
                "status": r["status"],
                "result": _json.loads(r["result"]) if r["result"] else None,
                "error": r["error"],
            })
        return results

    def _coerce_leader_decision(
        self,
        raw_decision: object,
        completed_results: list[dict],
    ) -> LeaderDecision:
        """Accept legacy decider output while making the stored outcome explicit."""
        if isinstance(raw_decision, LeaderDecision):
            return raw_decision
        if isinstance(raw_decision, DelegationDecision):
            return LeaderDecision(
                outcome=LeaderDecisionOutcome.ACTION,
                reason=raw_decision.reason,
                delegation_payload=raw_decision.model_dump(),
            )
        if raw_decision is None:
            if any(r["status"] == "completed" for r in completed_results):
                return LeaderDecision(
                    outcome=LeaderDecisionOutcome.DONE,
                    reason="completed member work satisfied the issue",
                )
            if self._all_completed_results_failed(completed_results):
                return LeaderDecision(
                    outcome=LeaderDecisionOutcome.FAILED,
                    reason="all completed member taskruns failed",
                )
            reason = "legacy decider returned no delegation"
            return LeaderDecision(outcome=LeaderDecisionOutcome.DONE, reason=reason)
        return LeaderDecision(
            outcome=LeaderDecisionOutcome.FAILED,
            reason=f"unsupported leader decision output: {type(raw_decision).__name__}",
            delegation_payload={"raw": repr(raw_decision)},
        )

    def _all_completed_results_failed(self, completed_results: list[dict]) -> bool:
        return bool(completed_results) and all(
            result["status"] == "failed" for result in completed_results
        )

    def _delegation_from_leader_decision(
        self, decision: LeaderDecision
    ) -> DelegationDecision | None:
        if not decision.delegation_payload:
            return None
        try:
            return DelegationDecision(**decision.delegation_payload)
        except Exception as exc:
            logger.error("invalid action delegation payload: %s", exc)
            return None

    def _record_failed_turn(
        self,
        task: Task,
        issue: Issue,
        reason: str,
        delegation_payload: dict | None = None,
        mark_issue_failed: bool = False,
    ) -> None:
        if task.squad_id is None:
            return
        record = self.store.record_leader_decision(
            issue_id=issue.id,
            squad_id=task.squad_id,
            leader_task_id=task.id,
            outcome=LeaderDecisionOutcome.FAILED,
            reason=reason,
            delegation_payload=delegation_payload or {},
            created_taskrun_ids=[],
        )
        if mark_issue_failed:
            self.store.update_issue_status(issue.id, IssueStatus.FAILED)
        self._ensure_leader_started(task.id)
        self.store.fail_task(
            task.id,
            f"leader decision {record.id}: {reason}",
            FailureReason.AGENT_ERROR,
        )

    def _complete_leader_task(self, task_id: str, record: LeaderDecision) -> None:
        self._ensure_leader_started(task_id)
        self.store.complete_task(
            task_id,
            {"leader_decision": record.model_dump(mode="json")},
        )

    def _ensure_leader_started(self, task_id: str) -> None:
        latest = self.store.get_task(task_id)
        if latest and latest.status in (TaskStatus.PREPARING, TaskStatus.CLAIMED):
            self.store.start_task(task_id)
