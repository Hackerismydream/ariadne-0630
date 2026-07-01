"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { createIssue, eventsUrl, listIssues, type NewIssueInput } from "../lib/api";
import type { IssueSummary } from "../lib/types";
import { AsciiLogo } from "./AsciiLogo";
import { AsciiProgress } from "./AsciiProgress";
import { Pane } from "./Pane";
import { ShellPrompt } from "./ShellPrompt";
import { IssueStatusBadge } from "./StatusBadge";

export function HomePage() {
  const router = useRouter();
  const [issues, setIssues] = useState<IssueSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const nextIssues = await listIssues();
        if (!cancelled) {
          setIssues(nextIssues);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    void load();
    const source = new EventSource(eventsUrl());
    const scheduleRefresh = () => {
      if (refreshTimer.current) {
        return;
      }
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null;
        void load();
      }, 200);
    };
    source.addEventListener("issue_timeline", scheduleRefresh);
    source.addEventListener("activity", scheduleRefresh);
    source.onerror = () => source.close();
    return () => {
      cancelled = true;
      if (refreshTimer.current) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
      source.close();
    };
  }, []);

  const activeTaskruns = useMemo(
    () => issues.reduce((total, issue) => total + issue.active_taskrun_count, 0),
    [issues],
  );

  async function submitTask(input: NewIssueInput) {
    setSubmitting(true);
    setError(null);
    try {
      const result = await createIssue(input);
      const issueId = result.issue_id ?? result.task_results[0]?.issue_id;
      if (!issueId) {
        throw new Error("backend did not return an issue id");
      }
      router.push(`/issues/${issueId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="terminal-shell">
      <div className="terminal-grid">
        <header className="grid gap-4 md:grid-cols-[1fr_auto] md:items-end">
          <AsciiLogo />
          <div className="grid gap-2 text-right text-terminal-muted">
            <AsciiProgress done={activeTaskruns} total={Math.max(activeTaskruns, issues.length)} label="agents working" />
            <button className="terminal-button justify-self-end" onClick={() => setModalOpen(true)}>
              [ NEW TASK ]
            </button>
          </div>
        </header>

        <Pane title="ISSUE LIST" right={<span>{loading ? "BOOTING" : `${issues.length} ISSUES`}</span>}>
          {error ? <div className="mb-4 text-terminal-error">ERR {error}</div> : null}
          <div className="grid gap-1">
            {issues.length === 0 ? (
              <div className="text-terminal-muted">no issues found. run [ NEW TASK ].</div>
            ) : (
              issues.map((issue) => (
                <Link
                  className="grid grid-cols-[13ch_1fr_16ch_12ch] gap-[2ch] border-b border-dashed border-terminal-border py-2 hover:bg-terminal-primary hover:text-terminal-bg hover:[text-shadow:none]"
                  href={`/issues/${issue.id}`}
                  key={issue.id}
                >
                  <IssueStatusBadge status={issue.status} />
                  <span className="truncate">{issue.title}</span>
                  <span>{issue.assignee_type}:{issue.assignee_id.slice(0, 8)}</span>
                  <span>active={issue.active_taskrun_count}</span>
                </Link>
              ))
            )}
          </div>
        </Pane>

        {modalOpen ? (
          <div className="fixed inset-0 z-40 grid place-items-center bg-black/80 p-4">
            <div className="w-full max-w-[86ch]">
              <Pane
                title="NEW TASK"
                right={
                  <button className="terminal-button" onClick={() => setModalOpen(false)} type="button">
                    [ CLOSE ]
                  </button>
                }
              >
                <ShellPrompt disabled={submitting} onSubmit={submitTask} />
                {submitting ? <div className="mt-4 text-terminal-secondary">POST /api/issues running...</div> : null}
              </Pane>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
