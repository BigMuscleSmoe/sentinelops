import { useState } from "react";
import type { BreakAnswer, BreakReason, Demo, Mode } from "../api/contract";
import { clock, hhmm, ms } from "../lib/format";
import { MODES, refusal } from "../lib/labels";
import { Card, CardHead } from "./ui";

interface Props {
  demo: Demo;
  viewer: BreakAnswer | null;
  onBreak: (mode: Mode) => void;
}

/** Why the button is off, if it is: the global state first, then this visitor's own last answer. */
function blockedBy(demo: Demo, viewer: BreakAnswer | null): { reason: BreakReason; retry: string | null } | null {
  if (demo.blocked === "paused") return { reason: "paused", retry: null };
  if (demo.blocked === "busy") return { reason: viewer?.ok ? "started" : "busy", retry: demo.retry_at };
  if (demo.blocked) return { reason: demo.blocked, retry: demo.retry_at };
  if (viewer && !viewer.ok && viewer.reason !== "unavailable" && viewer.retry_at && ms(viewer.retry_at) > Date.now()) {
    return { reason: viewer.reason, retry: viewer.retry_at };
  }
  return null;
}

function stateLine(demo: Demo, block: ReturnType<typeof blockedBy>, viewer: BreakAnswer | null): string {
  if (block?.reason === "started") {
    if (demo.phase === "settling") {
      return `Your break's run is over. Its incident closes after 10 quiet minutes; the next break can start about ${block.retry ? hhmm(block.retry) : "then"} UTC.`;
    }
    return demo.injected ? `Your break started at ${clock(demo.injected.at)} UTC. Watch it below.` : refusal("started", null);
  }
  if (block?.reason === "busy" && demo.phase === "settling") {
    return `The last break's incident closes after 10 quiet minutes. The next break can start about ${block.retry ? hhmm(block.retry) : "then"} UTC.`;
  }
  if (block?.reason === "busy") return "Someone's break is running now. Watch it below.";
  if (block) return refusal(block.reason, block.retry ? hhmm(block.retry) : null);
  if (viewer?.reason === "unavailable") return refusal("unavailable", null);
  return "Starts a real fault on the live service. The agent gets the alarm about 30 seconds later.";
}

export function BreakPanel({ demo, viewer, onBreak }: Props) {
  const [mode, setMode] = useState<Mode>("bad_deploy");
  const block = blockedBy(demo, viewer);
  const disabled = block !== null;
  const line = stateLine(demo, block, viewer);
  const tone =
    viewer?.reason === "unavailable" && !block ? "text-alarm" : block && block.reason !== "started" ? "text-warn" : "text-ink-2";

  return (
    <Card>
      <CardHead
        title="Break the service"
        aside={<span>checkout-api, a demo service with fake orders</span>}
      />
      <fieldset disabled={disabled} className="px-4 pt-3 pb-1">
        <legend className="sr-only">Choose a fault to inject</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {MODES.map((m) => {
            const checked = m.mode === mode;
            return (
              <label
                key={m.mode}
                className={`flex min-h-16 cursor-pointer flex-col gap-0.5 rounded-md border px-3 py-2.5 has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-link ${
                  checked ? "border-ink bg-sunk shadow-[inset_0_0_0_1px_var(--color-ink)]" : "border-line hover:border-line-strong"
                } ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
              >
                <input
                  type="radio"
                  name="mode"
                  value={m.mode}
                  checked={checked}
                  onChange={() => setMode(m.mode)}
                  className="sr-only"
                />
                <span className="flex items-center gap-2 text-[14px] font-medium">
                  <span
                    aria-hidden="true"
                    className={`grid size-4 place-items-center rounded-full border ${checked ? "border-ink" : "border-line-strong"}`}
                  >
                    {checked && <span className="size-2 rounded-full bg-ink" />}
                  </span>
                  {m.title}
                </span>
                <span className="pl-6 text-[13px] leading-snug text-muted">{m.line}</span>
              </label>
            );
          })}
        </div>
      </fieldset>
      <div className="flex flex-col gap-3 px-4 pt-3 pb-3 sm:flex-row sm:items-center">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onBreak(mode)}
          className="inline-flex h-11 w-full shrink-0 items-center justify-center gap-2 rounded-md bg-ink px-5 text-[15px] font-semibold text-white hover:bg-black active:translate-y-px disabled:cursor-not-allowed disabled:bg-line disabled:text-muted sm:w-auto"
        >
          <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.6">
            <path d="M9.5 1.5 4 9h4l-1.5 5.5L12 7H8l1.5-5.5Z" strokeLinejoin="round" />
          </svg>
          Break checkout-api
        </button>
        <p role="status" aria-live="polite" className={`text-[14px] leading-snug ${tone}`}>
          {line}
        </p>
      </div>
      <div className="space-y-1 border-t border-line px-4 py-3 text-[12.5px] leading-snug text-muted">
        <p>
          Limits: one break at a time; per visitor, 1 every 20 minutes and 3 a day; {demo.today.limit} a day in all (
          {demo.today.used} used today).
        </p>
        <p>
          Privacy: to apply the per-visitor limits, the demo keeps a salted hash of your IP address for 2 days, never
          the address itself.
        </p>
      </div>
    </Card>
  );
}
