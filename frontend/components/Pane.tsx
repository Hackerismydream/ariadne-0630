import type { ReactNode } from "react";

export function Pane({
  title,
  right,
  children,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="terminal-pane">
      <div className="terminal-pane__title">
        <span>+--- {title} ---+</span>
        {right ? <span>{right}</span> : null}
      </div>
      <div className="terminal-pane__body">{children}</div>
    </section>
  );
}
