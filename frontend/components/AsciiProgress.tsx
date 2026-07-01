import { asciiProgress } from "../lib/format";

export function AsciiProgress({
  done,
  total,
  label,
}: {
  done: number;
  total: number;
  label: string;
}) {
  return (
    <div className="text-terminal-primary" aria-label={`${label}: ${done}/${total}`}>
      <span>{asciiProgress(done, total)}</span>
      <span className="ml-[1ch] text-terminal-muted">{label}</span>
      <span className="ml-[1ch]">{done}/{total}</span>
    </div>
  );
}
