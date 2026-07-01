"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { eventsUrl, getIssue } from "../lib/api";
import type {
  ActivityStreamEvent,
  IssueDetail,
  IssueTimelineStreamEvent,
} from "../lib/types";
import { AsciiLogo } from "./AsciiLogo";
import { AsciiProgress } from "./AsciiProgress";
import { Pane } from "./Pane";
import { IssueStatusBadge, TaskStatusBadge } from "./StatusBadge";
import { Transcript, type TranscriptEvent } from "./Transcript";

export function IssueDetailPage({ issueId }: { issueId: string }) {
  const [detail, setDetail] = useState<IssueDetail | null>(null);
  const [events, setEvents] = useState<TranscriptEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seenEvents = useRef(new Set<string>());
  const taskrunIds = useMemo(() => new Set(detail?.taskruns.map((taskrun) => taskrun.id) ?? []), [detail]);
  const taskrunIdsRef = useRef(taskrunIds);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    taskrunIdsRef.current = taskrunIds;
  }, [taskrunIds]);

  const loadIssue = useCallback(async () => {
    try {
      const nextDetail = await getIssue(issueId);
      setDetail(nextDetail);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [issueId]);

  useEffect(() => {
    void loadIssue();
  }, [loadIssue]);

  useEffect(() => {
    if (!detail) {
      return;
    }
    let cancelled = false;
    const scheduleLoad = () => {
      if (refreshTimer.current) {
        return;
      }
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null;
        if (!cancelled) {
          void loadIssue();
        }
      }, 200);
    };
    const source = new EventSource(eventsUrl());
    const append = (event: TranscriptEvent) => {
      const key = `${event.type}:${event.id}`;
      if (seenEvents.current.has(key)) {
        return;
      }
      seenEvents.current.add(key);
      setEvents((current) => [...current, event].slice(-160));
    };
    source.addEventListener("issue_timeline", (message) => {
      const payload = JSON.parse(message.data) as Omit<IssueTimelineStreamEvent, "type">;
      if (payload.issue_id !== issueId) {
        return;
      }
      append({ ...payload, type: "issue_timeline" });
      scheduleLoad();
    });
    source.addEventListener("activity", (message) => {
      const payload = JSON.parse(message.data) as Omit<ActivityStreamEvent, "type">;
      if (!payload.task_id || !taskrunIdsRef.current.has(payload.task_id)) {
        return;
      }
      append({ ...payload, type: "activity" });
      scheduleLoad();
    });
    source.onerror = () => source.close();
    return () => {
      cancelled = true;
      if (refreshTimer.current) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
      source.close();
    };
  }, [detail?.id, issueId, loadIssue]);

  const completedTaskruns =
    detail?.taskruns.filter((taskrun) => taskrun.status === "completed").length ?? 0;
  const taskrunTotal = detail?.taskruns.length ?? 0;

  return (
    <main className="terminal-shell">
      <div className="terminal-grid">
        <header className="grid gap-4 md:grid-cols-[1fr_auto] md:items-end">
          <AsciiLogo />
          <Link className="terminal-button justify-self-start md:justify-self-end" href="/">
            [ ISSUE LIST ]
          </Link>
        </header>

        {error ? <Pane title="ERROR"><div className="text-terminal-error">{error}</div></Pane> : null}

        <Pane title={`ISSUE ${issueId}`} right={detail ? <IssueStatusBadge status={detail.status} /> : "LOADING"}>
          {detail ? (
            <div className="grid gap-2">
              <div>title    : {detail.title}</div>
              <div>assignee : {detail.assignee_type}:{detail.assignee_id}</div>
              <div>created  : {detail.created_at}</div>
              <AsciiProgress done={completedTaskruns} total={Math.max(taskrunTotal, 1)} label="taskruns completed" />
            </div>
          ) : (
            <div className="text-terminal-muted">loading issue snapshot...</div>
          )}
        </Pane>

        <Pane title="REALTIME TRANSCRIPT" right={<span>{events.length} EVENTS</span>}>
          <Transcript events={events} />
        </Pane>

        <Pane title="TASKRUNS">
          <div className="grid gap-2">
            {detail?.taskruns.map((taskrun) => (
              <div className="grid grid-cols-[13ch_1fr_10ch] gap-[2ch] border-b border-dashed border-terminal-border py-2" key={taskrun.id}>
                <TaskStatusBadge status={taskrun.status} />
                <span>{taskrun.id}</span>
                <span>{taskrun.duration_seconds ?? 0}s</span>
              </div>
            )) ?? <div className="text-terminal-muted">no taskruns yet</div>}
          </div>
        </Pane>

        <Pane title="DIFF / CHANGED FILES" right={<span>{detail?.changed_files.length ?? 0} FILES</span>}>
          {detail?.changed_files.length ? (
            <pre className="mb-4 overflow-auto text-terminal-secondary">
              {detail.changed_files.map((file) => `+ ${file}`).join("\n")}
            </pre>
          ) : (
            <div className="mb-4 text-terminal-muted">(no changed files captured)</div>
          )}
          <pre className="max-h-[34vh] overflow-auto border-l border-terminal-border pl-[2ch] text-[var(--fs-mono)] text-terminal-primary">
            {detail?.diff ?? "(no diff captured)"}
          </pre>
        </Pane>
      </div>
    </main>
  );
}
