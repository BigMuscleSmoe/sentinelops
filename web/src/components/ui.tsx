import { useId, useState, type ReactNode } from "react";
import type { Tone } from "../lib/labels";

const TONES: Record<Tone, string> = {
  plain: "border-line bg-sunk text-ink-2",
  ok: "border-ok/25 bg-ok-soft text-ok",
  warn: "border-warn/25 bg-warn-soft text-warn",
  alarm: "border-alarm/25 bg-alarm-soft text-alarm",
};

export function Badge({ tone = "plain", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded border px-1.5 py-0.5 text-[12px] leading-4 font-medium whitespace-nowrap ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Card({ id, className = "", children }: { id?: string; className?: string; children: ReactNode }) {
  return (
    <section id={id} className={`min-w-0 rounded-md border border-line bg-panel ${className}`}>
      {children}
    </section>
  );
}

export function CardHead({ title, aside, id }: { title: ReactNode; aside?: ReactNode; id?: string }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-3">
      <h2 id={id} className="text-[15px] font-semibold tracking-tight">
        {title}
      </h2>
      {aside && <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">{aside}</div>}
    </div>
  );
}

/** A labeled show/hide, keyboard and screen-reader friendly. */
export function Disclosure({
  label,
  children,
  initiallyOpen = false,
}: {
  label: ReactNode;
  children: ReactNode;
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  const id = useId();
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
        className="-mx-1 inline-flex min-h-8 items-center gap-1.5 rounded px-1 text-[13px] font-medium text-link hover:underline"
      >
        <Chevron open={open} />
        {label}
      </button>
      <div id={id} hidden={!open}>
        {open && children}
      </div>
    </div>
  );
}

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 12 12"
      className={`size-3 transition-transform ${open ? "rotate-90" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
    >
      <path d="M4.5 2.5 8 6l-3.5 3.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Dot({ tone }: { tone: Tone }) {
  const color = { plain: "bg-line-strong", ok: "bg-ok", warn: "bg-warn pulse-ring", alarm: "bg-alarm" }[tone];
  return <span aria-hidden="true" className={`inline-block size-2 shrink-0 rounded-full ${color}`} />;
}

/**
 * Model text with its `inline code` set in mono. The string is split on
 * backticks and every piece goes in as a text node: nothing is parsed as HTML.
 */
export function Prose({ text }: { text: string }) {
  const parts = text.split("`");
  if (parts.length % 2 === 0) return <>{text}</>; // an unpaired backtick: show it as written
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <code key={i} className="rounded-sm bg-sunk px-1 py-px font-mono text-[0.88em]">
            {part}
          </code>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="font-medium text-link hover:underline">
      {children}
    </a>
  );
}
