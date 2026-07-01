import { eventToTranscriptLine } from "../lib/format";
import type { ActivityStreamEvent, IssueTimelineStreamEvent } from "../lib/types";

export type TranscriptEvent = ActivityStreamEvent | IssueTimelineStreamEvent;

export function Transcript({ events }: { events: TranscriptEvent[] }) {
  return (
    <div
      role="log"
      aria-live="polite"
      className="max-h-[38vh] min-h-44 overflow-auto border-l border-terminal-border pl-[2ch] text-[var(--fs-mono)] leading-6"
    >
      {events.length === 0 ? (
        <div className="text-terminal-muted">~ waiting for persisted runtime events</div>
      ) : (
        events.map((event) => (
          <div className="type-line" key={`${event.type}:${event.id}`}>
            <span className={event.type === "activity" ? "text-terminal-primary" : "text-terminal-secondary"}>
              {eventToTranscriptLine(event)}
            </span>
          </div>
        ))
      )}
      <div className="blink text-terminal-primary">█</div>
    </div>
  );
}
