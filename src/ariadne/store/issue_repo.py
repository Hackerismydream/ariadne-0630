"""Issue and issue timeline persistence methods."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ariadne.models import (
    AssigneeType,
    Issue,
    IssueStatus,
    IssueTimelineEvent,
)

from .base import _new_id, _now_iso


_TERMINAL_ISSUE_STATUSES = (
    IssueStatus.DONE,
    IssueStatus.FAILED,
    IssueStatus.CANCELLED,
)


class IssueRepo:

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    def append_issue_timeline_event(
        self,
        issue_id: str,
        event_type: str,
        actor_type: str = "system",
        actor_id: str | None = None,
        taskrun_id: str | None = None,
        runtime_lease_id: str | None = None,
        leader_decision_id: str | None = None,
        comment_id: str | None = None,
        payload: dict | None = None,
    ) -> IssueTimelineEvent:
        event_id = _new_id("evt")
        created_at = _now_iso()
        self._conn.execute(
            """INSERT INTO issue_timeline_event
               (id, issue_id, event_type, actor_type, actor_id, taskrun_id,
                runtime_lease_id, leader_decision_id, comment_id, payload_json,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                issue_id,
                event_type,
                actor_type,
                actor_id,
                taskrun_id,
                runtime_lease_id,
                leader_decision_id,
                comment_id,
                json.dumps(payload or {}),
                created_at,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM issue_timeline_event WHERE id = ?", (event_id,)
        ).fetchone()
        return self.row_to(IssueTimelineEvent, row)

    def get_issue_timeline(self, issue_id: str) -> list[IssueTimelineEvent]:
        rows = self._conn.execute(
            """SELECT * FROM issue_timeline_event
               WHERE issue_id = ?
               ORDER BY created_at, id""",
            (issue_id,),
        ).fetchall()
        return [self.row_to(IssueTimelineEvent, r) for r in rows]

    def create_issue(
        self,
        title: str,
        description: str,
        assignee_type: AssigneeType,
        assignee_id: str,
    ) -> Issue:
        issue = Issue(
            id=_new_id("issue"),
            title=title,
            description=description,
            status=IssueStatus.BACKLOG,
            assignee_type=assignee_type,
            assignee_id=assignee_id,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            """INSERT INTO issue (id, title, description, status, assignee_type, assignee_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                issue.id,
                issue.title,
                issue.description,
                issue.status.value,
                issue.assignee_type.value,
                issue.assignee_id,
                issue.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        self.append_issue_timeline_event(
            issue.id,
            "issue_created",
            actor_type="user",
            payload={"title": issue.title},
        )
        return issue

    def get_issue(self, issue_id: str) -> Issue | None:
        row = self._conn.execute(
            "SELECT * FROM issue WHERE id = ?", (issue_id,)
        ).fetchone()
        return self.row_to(Issue, row) if row else None

    def list_issues(self) -> list[Issue]:
        rows = self._conn.execute("SELECT * FROM issue ORDER BY created_at").fetchall()
        return [self.row_to(Issue, r) for r in rows]

    def update_issue_status(self, issue_id: str, status: IssueStatus) -> Issue:
        self._conn.execute(
            "UPDATE issue SET status = ? WHERE id = ?",
            (status.value, issue_id),
        )
        self._conn.commit()
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(f"issue not found: {issue_id}")
        if status in _TERMINAL_ISSUE_STATUSES:
            self.append_issue_timeline_event(
                issue_id,
                "issue_closed",
                payload={"status": status.value},
            )
        return issue

    def update_issue(
        self,
        issue_id: str,
        *,
        status: IssueStatus | None = None,
        assignee_type: AssigneeType | None = None,
        assignee_id: str | None = None,
    ) -> Issue:
        existing = self.get_issue(issue_id)
        if existing is None:
            raise KeyError(f"issue not found: {issue_id}")
        next_status = status or existing.status
        next_assignee_type = assignee_type or existing.assignee_type
        next_assignee_id = assignee_id if assignee_id is not None else existing.assignee_id
        self._conn.execute(
            """UPDATE issue
               SET status = ?, assignee_type = ?, assignee_id = ?
               WHERE id = ?""",
            (
                next_status.value,
                next_assignee_type.value,
                next_assignee_id,
                issue_id,
            ),
        )
        self._conn.commit()
        if status is not None and status != existing.status:
            self.append_issue_timeline_event(
                issue_id,
                "issue_status_changed",
                payload={"status": next_status.value},
            )
            if next_status in _TERMINAL_ISSUE_STATUSES:
                self.append_issue_timeline_event(
                    issue_id,
                    "issue_closed",
                    payload={"status": next_status.value},
                )
        if (
            assignee_type is not None
            and assignee_type != existing.assignee_type
            or assignee_id is not None
            and assignee_id != existing.assignee_id
        ):
            self.append_issue_timeline_event(
                issue_id,
                "issue_assignee_changed",
                payload={
                    "assignee_type": next_assignee_type.value,
                    "assignee_id": next_assignee_id,
                },
            )
        updated = self.get_issue(issue_id)
        if updated is None:
            raise KeyError(f"issue not found: {issue_id}")
        return updated

    def list_issue_timeline_events_after(
        self,
        *,
        created_at: str | None = None,
        event_id: str | None = None,
        limit: int = 100,
    ) -> list[IssueTimelineEvent]:
        if created_at is None or event_id is None:
            rows = self._conn.execute(
                """SELECT * FROM issue_timeline_event
                   ORDER BY created_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM issue_timeline_event
                   WHERE created_at > ?
                      OR (created_at = ? AND id > ?)
                   ORDER BY created_at, id LIMIT ?""",
                (created_at, created_at, event_id, limit),
            ).fetchall()
        return [self.row_to(IssueTimelineEvent, r) for r in rows]
