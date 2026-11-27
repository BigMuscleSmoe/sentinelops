// SentinelOps' public API, version 1: everything the page reads and sends.
// Approved 2026-11-26 (Phase 5, evening 1). The demo functions' Pydantic models
// mirror these types, and a test compares the two.
//
// Routes, all behind one CloudFront distribution (no other origin is reachable):
//
//   GET  /api/live              Live           max-age=2 while a break or run is live, else 15
//   GET  /api/incidents         Incidents      max-age=15
//   GET  /api/incidents/{id}    IncidentDetail max-age=2 while open, 300 once closed
//   POST /api/break             BreakAnswer    no-store
//
// Edge rules (a CloudFront Function, before any cache or function):
// - Any other path is 404, any other method 405, each with an ErrorBody.
// - The query string and cookies are dropped on /api/*, so they never reach a
//   function or a cache key.
// - POST /api/break needs Content-Type application/json, a body of at most 64
//   bytes, and x-amz-content-sha256 (the body's SHA-256, hex), which OAC needs.
// - The plan's per-IP rule blocks an address past 1,000 requests in 5 minutes.
// - Every response gets the security headers (the CSP in vite.config.ts).
//
// Text rules, both sides:
// - Every string that holds model output, a tool's result, log text, or code is
//   plain text. The page renders it as text nodes only, never as HTML.
// - Each such field is capped (TEXT_CHARS) and scanned before it is stored,
//   with the remediation flow's rules (the commit hooks' rules plus no ARN). A
//   field with a finding is stored as "[withheld: <rule>]".
// - Nothing a visitor sends appears in any response, log line, or stored item,
//   except the mode, as one of the three values below.

export const API_VERSION = 1;
export const TEXT_CHARS = 4_000; // per text field in the trace; longer results are cut
export const MAX_INCIDENTS = 20;
export const MAX_BREAK_BODY_BYTES = 64;
export const REPO_PR_URL = /^https:\/\/github\.com\/BigMuscleSmoe\/sentinelops\/pull\/[1-9][0-9]{0,5}$/;
export const INCIDENT_ID = /^checkout-api-[0-9]{8}T[0-9]{6}Z$/;

/** ISO 8601 in UTC, ending in Z. */
export type Iso = string;
/** checkout-api-YYYYMMDDTHHMMSSZ, the time its first alarm fired. */
export type IncidentId = string;

export type Mode = "bad_deploy" | "db_pool_exhaustion" | "downstream_timeout";
export type AlarmKey = "errors" | "latency" | "pool_wait";
export type ToolName = "get_metrics" | "get_recent_logs" | "get_deploy_history" | "search_runbooks" | "read_repo_file";

// ---- GET /api/live ---------------------------------------------------------

export interface Live {
  v: 1;
  as_of: Iso;
  demo: Demo;
  /** The open incident, else the newest one with a trace. */
  incident: IncidentDetail | null;
  /** Only while a break's fault is active or its incident is open. */
  metrics: Metrics | null;
}

export type Phase =
  | "ready" //         the button works
  | "fault_active" //  a break is injected; no alarm yet
  | "investigating" // the agent is running
  | "remediating" //   the flow is opening or checking a PR
  | "settling" //      the run is over; the incident closes after 10 quiet minutes
  | "paused"; //       the kill switch is on

/**
 * The demo's global state. This document is cached and the same for every
 * visitor, so it holds only global limits. A visitor's own limits (cooldown,
 * visitor_day) come only in POST /api/break's answer, and the page keeps that
 * answer's retry_at in the browser.
 */
export interface Demo {
  phase: Phase;
  blocked: "busy" | "daily" | "monthly" | "paused" | null;
  /** When the button is expected to work again, when known. */
  retry_at: Iso | null;
  /** The live break, if any. */
  injected: Injected | null;
  today: { used: number; limit: number }; // breaks started today (UTC)
}

export interface Injected {
  mode: Mode;
  at: Iso;
  /** The fault ends by itself 390 s after it starts. */
  fault_ends_at: Iso;
}

// ---- GET /api/incidents ----------------------------------------------------

export interface Incidents {
  v: 1;
  as_of: Iso;
  /** Newest first, at most MAX_INCIDENTS: those with a trace, and the recorded run of 2026-11-26. */
  incidents: IncidentSummary[];
}

export type Status = "investigating" | "answered" | "halted" | "failed" | "refused";

export interface IncidentSummary {
  id: IncidentId;
  service: "checkout-api";
  opened_at: Iso;
  /** 10 quiet minutes after its last alarm went OK; null while open. */
  closed_at: Iso | null;
  /** In firing order. Labels come from the alarms' Terraform thresholds. */
  alarms: AlarmMark[];
  status: Status;
  /** The one line, built in code at read time from the typed fields here and in the answer and PR. No model writes it. */
  summary: string;
  run: RunStats | null;
  /** The demo break that caused it, if one did, from the demo's own table (which the agent never reads). */
  injected: (Injected & { runbook_matches: boolean | null }) | null;
  pr_state: PrState;
}

export interface AlarmMark {
  alarm: AlarmKey;
  label: string; // "Errors over 5%"
  at: Iso;
  ok_at: Iso | null;
}

export interface RunStats {
  seconds: number; // alarm's run, start to answer
  tool_calls: number;
  model_calls: number;
  input_tokens: number;
  cost_usd: number; // the model's cost; AWS's isn't metered per run
}

// ---- GET /api/incidents/{id} -----------------------------------------------

export interface IncidentDetail extends IncidentSummary {
  trace: TraceEvent[];
  answer: Answer | null;
  pr: Pr | null; // null until the flow has the answer
}

/** One event per step, in order: seq starts at 1 and has no gaps. */
export type TraceEvent = { seq: number; at: Iso } & (
  | { kind: "run_started"; alarm: AlarmKey }
  | { kind: "model_call"; n: number; input_tokens: number; output_tokens: number; cost_usd: number }
  | { kind: "thinking"; text: string } // summarized by the model's API
  | { kind: "said"; text: string } // the agent's words between tool calls
  | { kind: "tool_call"; call: string; tool: ToolName; args: Record<string, string | number | boolean> }
  | { kind: "tool_result"; call: string; source: string; chars: number; text: string; truncated: boolean; error: boolean }
  | { kind: "check"; check: "citation" | "patch"; kept: boolean; detail: string } // detail from fixed templates
  | { kind: "answered"; confidence: number }
  | { kind: "halted"; by: HaltReason }
  | { kind: "handed_off" }
  | { kind: "pr_opened"; number: number }
  | { kind: "pr_same_fix"; number: number; commented: boolean } // the same fix was already open; a comment at most once a day
  | { kind: "pr_checks"; check: string; state: "pending" | "success" | "failure"; poll: number }
  | { kind: "pr_ready"; number: number }
  | { kind: "pr_rejected"; check: string } // the flow's own check refused the patch
  | { kind: "flow_failed"; error: string } // an error's type or code, never its message
);

export type HaltReason = "tool_calls" | "input_tokens" | "cost" | "wall_clock" | "resends";

// ---- The answer (the Hypothesis, as shown) ---------------------------------

export interface Answer {
  root_cause: string;
  confidence: number;
  fault_location: "this_service" | "upstream" | "undetermined";
  affected_component: string;
  running_version: string | null;
  runbook: { file: string; title: string } | null;
  evidence: Evidence[]; // ids e1, e2, ... in order
  inferences: { claim: string; rests_on: string[] }[];
  not_checked: string[];
}

export interface Evidence {
  id: string;
  kind: "value" | "range" | "all" | "count" | "first" | "last" | "none" | "gap";
  source: string; // m1, l2, d1, r1, c1, ...
  column: string | null;
  at: string | null;
  start: string | null;
  end: string | null;
  lines: number[] | null;
  value: string | null;
  low: number | null;
  high: number | null;
  count: number | null;
  seconds: number | null;
  note: string;
}

// ---- The PR, from the incident's record (no GitHub call when the page loads) ----

export type PrState =
  | "none" //           no PR: see blockers
  | "opening" //        the flow has the answer
  | "draft" //          opened; CI not yet seen
  | "checks_running"
  | "ready" //          CI passed; marked ready for review
  | "checks_failed"
  | "rejected" //       the flow's own check refused the patch
  | "same_fix_open" //  commented on the open PR instead
  | "failed"; //        the flow failed

/** Why no PR: the conditions Hypothesis.pr_blockers() returns, in its order. */
export type Blocker = "halted" | "fault_elsewhere" | "low_confidence" | "no_patch" | "patch_unchecked" | "wrong_release";

export interface Pr {
  state: PrState;
  blockers: Blocker[]; // empty unless state is "none"
  number: number | null;
  url: string | null; // set only when it matches REPO_PR_URL
  title: string | null;
  branch: string | null;
  base: string | null;
  check: { name: string; conclusion: "pending" | "success" | "failure" } | null;
  file: string | null;
  release: string | null;
  diff: string | null; // unified diff, as text
  additions: number;
  deletions: number;
  rationale: string | null;
  /** The flow's finish. A merge or close after it isn't shown. */
  as_of: Iso | null;
}

// ---- Metrics, while a break is live ----------------------------------------

export interface Metrics {
  period_s: 10;
  from: Iso; // 3 minutes before `to`
  to: Iso; // the newest period's start
  series: Series[]; // errors, latency, pool_wait, in that order
}

export interface Series {
  alarm: AlarmKey;
  label: string; // "Error rate"
  unit: "%" | "ms";
  threshold: number; // in `unit`
  points: (number | null)[]; // one per period from `from`; null where no request landed
}

// ---- POST /api/break -------------------------------------------------------

export interface BreakRequest {
  mode: Mode;
}

export type BreakReason =
  | "started" //      200
  | "busy" //         409: a fault is active, an incident is open, or a run holds the lock
  | "cooldown" //     429: this visitor's last break was under 20 minutes ago
  | "visitor_day" //  429: this visitor's 3 today are used
  | "daily" //        429: today's 20 are used
  | "monthly" //      429: this month's 200 are used
  | "paused" //       503: the kill switch is on
  | "unavailable"; // 503: something failed on our side; nothing was started

export interface BreakAnswer {
  ok: boolean; // true only for "started"
  reason: BreakReason;
  retry_at: Iso | null;
  injected: Injected | null; // set when started
}

// ---- Errors, every route ---------------------------------------------------

/** The whole body of any 4xx or 5xx that isn't a BreakAnswer. It never repeats the request. */
export interface ErrorBody {
  error: "invalid" | "not_found" | "method" | "too_large" | "unavailable";
}
