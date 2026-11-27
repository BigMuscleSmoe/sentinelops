import type { BreakReason, Blocker, Mode, PrState, ToolName } from "../api/contract";

export const MODES: { mode: Mode; title: string; line: string }[] = [
  { mode: "bad_deploy", title: "Bad deploy", line: "Ships a release with a pricing bug. Ends in a fix PR." },
  { mode: "db_pool_exhaustion", title: "Pool exhaustion", line: "Holds database connections until checkouts queue." },
  { mode: "downstream_timeout", title: "Slow downstream", line: "Slows payments-api until checkouts time out." },
];

export const modeTitle = (mode: Mode): string => MODES.find((m) => m.mode === mode)?.title ?? mode;

export const BLOCKERS: Record<Blocker, string> = {
  halted: "The run halted at a limit before it finished.",
  fault_elsewhere: "The fault is outside this service, so there is nothing to fix in its code.",
  low_confidence: "The agent's confidence is under 0.7.",
  no_patch: "The agent proposed no code fix: this fault isn't in the code.",
  patch_unchecked: "The patch failed the run's check and was dropped.",
  wrong_release: "The patch is for a release that wasn't running.",
};

export const PR_STATE: Record<PrState, { label: string; tone: Tone }> = {
  none: { label: "No PR", tone: "plain" },
  opening: { label: "Opening", tone: "warn" },
  draft: { label: "Draft, waiting on CI", tone: "warn" },
  checks_running: { label: "CI running", tone: "warn" },
  ready: { label: "Ready for review", tone: "ok" },
  checks_failed: { label: "CI failed", tone: "alarm" },
  rejected: { label: "Patch rejected", tone: "alarm" },
  same_fix_open: { label: "Same fix already open", tone: "ok" },
  failed: { label: "Flow failed", tone: "alarm" },
};

export type Tone = "plain" | "ok" | "warn" | "alarm";

export const TOOLS: Record<ToolName, string> = {
  get_metrics: "Read metrics",
  get_recent_logs: "Read logs",
  get_deploy_history: "Read deploy history",
  search_runbooks: "Search runbooks",
  read_repo_file: "Read code",
};

export const WHERE = {
  this_service: "This service",
  upstream: "Upstream",
  undetermined: "Undetermined",
} as const;

/** What the button's line says for each answer a break can get. */
export function refusal(reason: BreakReason, retryClock: string | null): string {
  const at = retryClock ? `${retryClock} UTC` : "later";
  switch (reason) {
    case "started":
      return "Your break started. Watch it below.";
    case "busy":
      return `Another break's incident is still open. The next one can start about ${at}.`;
    case "cooldown":
      return `You can start another break at ${at}: one every 20 minutes per visitor.`;
    case "visitor_day":
      return "You've started 3 breaks today, the most per visitor. More from 00:00 UTC.";
    case "daily":
      return "Today's 20 breaks are used. More from 00:00 UTC.";
    case "monthly":
      return "This month's 200 breaks are used. More on the 1st.";
    case "paused":
      return "Breaks are paused for now. The latest run plays below.";
    case "unavailable":
      return "Something failed on our side and nothing was started. Try again in a minute.";
  }
}
