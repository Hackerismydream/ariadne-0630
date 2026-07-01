"""Squad, leader decision, activity, and timeline persistence methods."""

from __future__ import annotations

import json

from ariadne.models import Agent, LeaderDecision, LeaderDecisionOutcome, Squad, SquadMember

from .base import _new_id, _now_iso


class SquadRepo:

    def record_leader_decision(
        self,
        issue_id: str,
        squad_id: str,
        leader_task_id: str,
        outcome: LeaderDecisionOutcome,
        reason: str = "",
        delegation_payload: dict | None = None,
        created_taskrun_ids: list[str] | None = None,
    ) -> LeaderDecision:
        decision_id = _new_id("leaderdecision")
        created_at = _now_iso()
        self._conn.execute(
            """INSERT INTO leader_decision
               (id, issue_id, squad_id, leader_task_id, outcome, reason,
                delegation_payload_json, created_taskrun_ids_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                issue_id,
                squad_id,
                leader_task_id,
                outcome.value,
                reason,
                json.dumps(delegation_payload or {}),
                json.dumps(created_taskrun_ids or []),
                created_at,
            ),
        )
        self._conn.commit()
        decision = self.get_leader_decision(decision_id)
        if decision is None:
            raise KeyError(f"leader decision not found: {decision_id}")
        self.append_issue_timeline_event(
            issue_id,
            "leader_decided",
            actor_type="leader",
            taskrun_id=leader_task_id,
            leader_decision_id=decision.id,
            payload={
                "outcome": outcome.value,
                "reason": reason,
                "delegation_payload": delegation_payload or {},
                "created_taskrun_ids": created_taskrun_ids or [],
            },
        )
        return decision

    def get_leader_decision(self, decision_id: str) -> LeaderDecision | None:
        row = self._conn.execute(
            "SELECT * FROM leader_decision WHERE id = ?", (decision_id,)
        ).fetchone()
        return self.row_to(LeaderDecision, row) if row else None

    def list_leader_decisions(self, issue_id: str | None = None) -> list[LeaderDecision]:
        if issue_id is None:
            rows = self._conn.execute(
                "SELECT * FROM leader_decision ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM leader_decision
                   WHERE issue_id = ?
                   ORDER BY created_at, id""",
                (issue_id,),
            ).fetchall()
        return [self.row_to(LeaderDecision, r) for r in rows]

    # ------------------------------------------------------------------
    # Squad
    # ------------------------------------------------------------------

    def create_squad(
        self, name: str, leader_id: str, instructions: str = ""
    ) -> Squad:
        squad = Squad(
            id=_new_id("squad"),
            name=name,
            leader_id=leader_id,
            instructions=instructions,
        )
        self._conn.execute(
            """INSERT INTO squad (id, name, leader_id, instructions)
               VALUES (?, ?, ?, ?)""",
            (squad.id, squad.name, squad.leader_id, squad.instructions),
        )
        self._conn.commit()
        return squad

    def add_squad_member(
        self, squad_id: str, member_id: str, role: str
    ) -> SquadMember:
        sm = SquadMember(
            squad_id=squad_id,
            member_type="agent",
            member_id=member_id,
            role=role,
        )
        self._conn.execute(
            """INSERT INTO squad_member (id, squad_id, member_type, member_id, role)
               VALUES (?, ?, ?, ?, ?)""",
            (_new_id("sm"), sm.squad_id, sm.member_type, sm.member_id, sm.role),
        )
        self._conn.commit()
        return sm

    def get_squad(self, squad_id: str) -> Squad | None:
        row = self._conn.execute(
            "SELECT * FROM squad WHERE id = ?", (squad_id,)
        ).fetchone()
        return self.row_to(Squad, row) if row else None

    def get_squad_members(self, squad_id: str) -> list[SquadMember]:
        rows = self._conn.execute(
            "SELECT * FROM squad_member WHERE squad_id = ?", (squad_id,)
        ).fetchall()
        return [self.row_to(SquadMember, r) for r in rows]

    def get_squad_leader(self, squad_id: str) -> Agent:
        squad = self.get_squad(squad_id)
        if squad is None:
            raise KeyError(f"squad not found: {squad_id}")
        agent = self.get_agent(squad.leader_id)
        if agent is None:
            raise KeyError(f"leader agent not found: {squad.leader_id}")
        return agent

    # ------------------------------------------------------------------
    # Activity log / trace
    # ------------------------------------------------------------------

    def log_activity(
        self,
        trace_id: str,
        task_id: str | None,
        event: str,
        details: dict | None = None,
    ) -> None:
        """Write an activity log entry for a trace."""
        self._conn.execute(
            """INSERT INTO activity_log (id, trace_id, task_id, event, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _new_id("act"),
                trace_id,
                task_id,
                event,
                json.dumps(details) if details else None,
                _now_iso(),
            ),
        )
        self._conn.commit()

    def get_timeline(self, trace_id: str) -> list[dict]:
        """Return activity log entries for a trace, ordered by time."""
        rows = self._conn.execute(
            "SELECT * FROM activity_log WHERE trace_id = ? ORDER BY created_at",
            (trace_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "trace_id": r["trace_id"],
                "task_id": r["task_id"],
                "event": r["event"],
                "details": json.loads(r["details"]) if r["details"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ]
