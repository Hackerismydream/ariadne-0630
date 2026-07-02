"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { cancelIssue, eventsUrl, getIssue } from "../lib/api";
import {
  hasCancellableTaskruns,
  retryTreeLines,
  taskrunDiffExplanation,
} from "../lib/format";
import type {
  ActivityStreamEvent,
  IssueDetail,
  TaskRun,
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
  const [cancelling, setCancelling] = useState(false);
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
  const canCancel = detail ? hasCancellableTaskruns(detail.taskruns) : false;
  const retryLines = useMemo(() => (detail ? retryTreeLines(detail.taskruns) : []), [detail]);
  const diffExplanation = useMemo(() => {
    if (!detail) {
      return "(no diff captured)";
    }
    return detail.taskruns.map(taskrunDiffExplanation).find(Boolean) ?? "(no diff captured)";
  }, [detail]);

  const cancelCurrentIssue = useCallback(async () => {
    setCancelling(true);
    setError(null);
    try {
      await cancelIssue(issueId);
      await loadIssue();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCancelling(false);
    }
  }, [issueId, loadIssue]);

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

        <Pane
          title={`ISSUE ${issueId}`}
          right={detail ? (
            <div className="flex items-center gap-[2ch]">
              <IssueStatusBadge status={detail.status} />
              {canCancel ? (
                <button
                  className="terminal-button"
                  disabled={cancelling}
                  onClick={() => void cancelCurrentIssue()}
                  type="button"
                >
                  [ CANCEL ]
                </button>
              ) : null}
            </div>
          ) : "LOADING"}
        >
          {detail ? (
            <div className="grid gap-2">
              <div>title    : {detail.title}</div>
              <div>assignee : {detail.assignee_type}:{detail.assignee_id}</div>
              <div>created  : {detail.created_at}</div>
              {detail.status === "failed" ? (
                <div className="text-terminal-error">[ERR] issue failed; inspect taskrun chain below</div>
              ) : null}
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
              <div className="grid gap-1 border-b border-dashed border-terminal-border py-2" key={taskrun.id}>
                <div className="grid grid-cols-[13ch_1fr] gap-[2ch]">
                  <TaskStatusBadge status={taskrun.status} />
                  <span>{taskrun.id}</span>
                </div>
                <div className="grid gap-1 pl-[15ch] text-terminal-muted">
                  <span>attempt={taskrun.attempt}/{taskrun.max_attempts}</span>
                  <span>parent={taskrun.parent_taskrun_id ?? "root"}</span>
                  <span>elapsed={taskrunElapsed(taskrun)} last_event={taskrunLastEvent(taskrun)}</span>
                  {taskrun.failure_reason ? (
                    <span className="text-terminal-error">failure_reason={taskrun.failure_reason}</span>
                  ) : null}
                  {taskrunDiffExplanation(taskrun) ? (
                    <span className={taskrun.status === "failed" ? "text-terminal-error" : "text-terminal-muted"}>
                      {taskrunDiffExplanation(taskrun)}
                    </span>
                  ) : null}
                </div>
              </div>
            )) ?? <div className="text-terminal-muted">no taskruns yet</div>}
            {retryLines.length ? (
              <pre className="border-l border-terminal-border pl-[2ch] text-terminal-secondary">
                {retryLines.join("\n")}
              </pre>
            ) : null}
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
            {detail?.diff ?? diffExplanation}
          </pre>
        </Pane>
      </div>
    </main>
  );
}

function taskrunElapsed(taskrun: TaskRun): string {
  if (taskrun.duration_seconds === null) {
    return "n/a";
  }
  return `${Number.isInteger(taskrun.duration_seconds) ? taskrun.duration_seconds : taskrun.duration_seconds.toFixed(2)}s`;
}

function taskrunLastEvent(taskrun: TaskRun): string {
  return taskrun.completed_at ?? taskrun.started_at ?? taskrun.created_at;
}
