import type { IssueDetail, IssueSummary, RunResult } from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type NewIssueInput = {
  title: string;
  description: string;
  backend: "dry-run" | "codex" | "claude";
  mode: "direct" | "squad";
};

export function eventsUrl(): string {
  return `${API_BASE_URL}/api/events`;
}

export async function listIssues(): Promise<IssueSummary[]> {
  return apiFetch<IssueSummary[]>("/api/issues");
}

export async function getIssue(issueId: string): Promise<IssueDetail> {
  return apiFetch<IssueDetail>(`/api/issues/${issueId}`);
}

export async function createIssue(input: NewIssueInput): Promise<RunResult> {
  return apiFetch<RunResult>("/api/issues", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }
  return response.json() as Promise<T>;
}
