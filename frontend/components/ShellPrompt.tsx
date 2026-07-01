"use client";

import { useState } from "react";

import type { NewIssueInput } from "../lib/api";

export function ShellPrompt({
  disabled,
  onSubmit,
}: {
  disabled: boolean;
  onSubmit: (input: NewIssueInput) => Promise<void>;
}) {
  const [description, setDescription] = useState("");
  const [backend, setBackend] = useState<NewIssueInput["backend"]>("dry-run");
  const [mode, setMode] = useState<NewIssueInput["mode"]>("direct");

  const title = description.trim().split("\n")[0]?.slice(0, 80) || "Ariadne task";

  return (
    <form
      className="grid gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!description.trim() || disabled) {
          return;
        }
        void onSubmit({ title, description: description.trim(), backend, mode });
      }}
    >
      <label className="grid gap-2">
        <span className="text-terminal-muted">ariadne@run:~$</span>
        <textarea
          className="min-h-32 resize-y border-0 border-l border-terminal-border bg-transparent p-3 text-terminal-primary outline-none"
          value={description}
          disabled={disabled}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="describe the agent task"
          aria-label="Task description"
        />
      </label>
      <div className="flex flex-wrap items-center gap-[2ch] text-terminal-muted">
        <label>
          --backend{" "}
          <select
            className="border border-terminal-border bg-terminal-bg px-[1ch] py-1 text-terminal-primary"
            value={backend}
            disabled={disabled}
            onChange={(event) => setBackend(event.target.value as NewIssueInput["backend"])}
          >
            <option value="dry-run">dry-run</option>
            <option value="codex">codex</option>
            <option value="claude">claude</option>
          </select>
        </label>
        <label>
          --mode{" "}
          <select
            className="border border-terminal-border bg-terminal-bg px-[1ch] py-1 text-terminal-primary"
            value={mode}
            disabled={disabled}
            onChange={(event) => setMode(event.target.value as NewIssueInput["mode"])}
          >
            <option value="direct">direct</option>
            <option value="squad">squad</option>
          </select>
        </label>
        <button className="terminal-button" type="submit" disabled={disabled || !description.trim()}>
          [ RUN ]
        </button>
        <span className="blink text-terminal-primary">█</span>
      </div>
    </form>
  );
}
