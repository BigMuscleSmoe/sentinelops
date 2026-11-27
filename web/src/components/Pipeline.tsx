import type { Demo, IncidentDetail, TraceEvent } from "../api/contract";
import { clock, since } from "../lib/format";
import { modeTitle, WHERE } from "../lib/labels";

type StepState = "done" | "active" | "pending" | "stopped";

interface Step {
  label: string;
  value: string;
  at: string | null;
  state: StepState;
}

const first = (trace: TraceEvent[], kinds: TraceEvent["kind"][]) => trace.find((e) => kinds.includes(e.kind))?.at ?? null;

function steps(incident: IncidentDetail | null, demo: Demo): Step[] {
  const injected = incident?.injected ?? demo.injected;
  const trace = incident?.trace ?? [];
  const tools = trace.filter((e) => e.kind === "tool_call").length;
  const answer = incident?.answer ?? null;
  const pr = incident?.pr ?? null;
  const status = incident?.status;
  const alarm = incident?.alarms[0] ?? null;
  const answeredAt = first(trace, ["answered", "halted"]);
  const prAt = first(trace, ["pr_opened", "pr_same_fix", "pr_rejected", "flow_failed"]);
  const readyAt = first(trace, ["pr_ready"]) ?? (pr?.state === "same_fix_open" ? prAt : null);

  const investigation: Step =
    status === "investigating"
      ? { label: "Investigation", value: `${tools} tool call${tools === 1 ? "" : "s"} so far`, at: null, state: "active" }
      : status === "answered" && incident?.run
        ? { label: "Investigation", value: `${incident.run.tool_calls} tool calls, ${Math.round(incident.run.seconds)} s`, at: answeredAt, state: "done" }
        : status
          ? { label: "Investigation", value: status === "halted" ? "Halted at a limit" : "Didn't run", at: answeredAt, state: "stopped" }
          : { label: "Investigation", value: "Starts on the alarm", at: null, state: "pending" };

  const fix: Step = (() => {
    if (!answer) return { label: "Fix PR", value: "If the bug is in the code", at: null, state: "pending" as const };
    if (!pr) return { label: "Fix PR", value: "Opening", at: null, state: "active" as const };
    switch (pr.state) {
      case "opening":
        return { label: "Fix PR", value: "Opening", at: null, state: "active" as const };
      case "none":
        return { label: "Fix PR", value: "No PR", at: null, state: "stopped" as const };
      case "same_fix_open":
        return { label: "Fix PR", value: `Already open: #${pr.number}`, at: prAt, state: "done" as const };
      case "rejected":
      case "failed":
        return { label: "Fix PR", value: pr.state === "rejected" ? "Patch rejected" : "Flow failed", at: prAt, state: "stopped" as const };
      default:
        return { label: "Fix PR", value: `#${pr.number} opened`, at: prAt, state: "done" as const };
    }
  })();

  const review: Step = (() => {
    switch (pr?.state) {
      case "ready":
        return { label: "Ready for review", value: "CI passed", at: readyAt, state: "done" as const };
      case "same_fix_open":
        return { label: "Ready for review", value: `PR #${pr.number} already is`, at: readyAt, state: "done" as const };
      case "draft":
      case "checks_running":
        return { label: "Ready for review", value: "Waiting on CI", at: null, state: "active" as const };
      case "checks_failed":
        return { label: "Ready for review", value: "CI failed", at: null, state: "stopped" as const };
      case "none":
      case "rejected":
      case "failed":
        return { label: "Ready for review", value: "Nothing to review", at: null, state: "stopped" as const };
      default:
        return { label: "Ready for review", value: "A person merges", at: null, state: "pending" as const };
    }
  })();

  return [
    injected
      ? { label: "Fault injected", value: modeTitle(injected.mode), at: injected.at, state: "done" }
      : { label: "Fault", value: incident ? "A real alarm" : "Pick one above", at: null, state: incident ? "done" : "pending" },
    alarm
      ? { label: "Alarm", value: alarm.label, at: alarm.at, state: "done" }
      : { label: "Alarm", value: injected ? "Waiting for 2 of 3 bad periods" : "Fires on the fault", at: null, state: injected ? "active" : "pending" },
    investigation,
    answer
      ? { label: "Diagnosis", value: `${WHERE[answer.fault_location]}, ${answer.confidence.toFixed(2)}`, at: answeredAt, state: "done" }
      : { label: "Diagnosis", value: "With cited evidence", at: null, state: "pending" },
    fix,
    review,
  ];
}

function Marker({ state }: { state: StepState }) {
  const base = "relative z-10 grid size-4 shrink-0 place-items-center rounded-full border";
  switch (state) {
    case "done":
      return (
        <span className={`${base} border-ink bg-ink`}>
          <svg aria-hidden="true" viewBox="0 0 10 10" className="size-2.5" fill="none" stroke="#fff" strokeWidth="1.8">
            <path d="m2.2 5.2 1.8 1.8 3.8-4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      );
    case "active":
      return <span className={`${base} pulse-ring border-warn bg-warn-soft`} />;
    case "stopped":
      return (
        <span className={`${base} border-line-strong bg-sunk`}>
          <span className="h-0.5 w-2 rounded bg-muted" />
        </span>
      );
    case "pending":
      return <span className={`${base} border-line-strong bg-panel`} />;
  }
}

const SR_STATE: Record<StepState, string> = { done: "done", active: "in progress", pending: "not yet", stopped: "stopped" };

export function Pipeline({ incident, demo }: { incident: IncidentDetail | null; demo: Demo }) {
  const all = steps(incident, demo);
  const base = incident?.injected?.at ?? demo.injected?.at ?? incident?.alarms[0]?.at ?? null;
  return (
    <ol className="grid grid-cols-1 gap-0 px-4 py-4 sm:grid-cols-6 sm:gap-2">
      {all.map((s, n) => (
        <li key={s.label} className="relative flex gap-3 pb-4 last:pb-0 sm:flex-col sm:gap-2 sm:pb-0">
          {n < all.length - 1 && (
            <span
              aria-hidden="true"
              className={`absolute top-4 bottom-0 left-[7.5px] w-px sm:top-[7.5px] sm:right-[-8px] sm:bottom-auto sm:left-4 sm:h-px sm:w-auto ${
                s.state === "done" ? "bg-ink" : "bg-line-strong"
              }`}
            />
          )}
          <Marker state={s.state} />
          <div className="flex min-w-0 flex-1 items-baseline justify-between gap-3 sm:block">
            <div className="min-w-0">
              <p className="text-[11.5px] font-medium tracking-wide text-muted uppercase">
                {s.label}
                <span className="sr-only">, {SR_STATE[s.state]}</span>
              </p>
              <p className={`mt-0.5 text-[14px] leading-snug ${s.state === "pending" ? "text-muted" : "font-medium"}`}>
                {s.value}
              </p>
            </div>
            {s.at && base && (
              <p className="shrink-0 font-mono text-[12px] text-muted sm:mt-0.5" title={`${clock(s.at)} UTC`}>
                {since(base, s.at)}
                <span className="hidden sm:inline"> {clock(s.at)}</span>
              </p>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
