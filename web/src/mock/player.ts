// The mockup's stand-in for the API: it builds each Live document the page
// would get from GET /api/live, at any moment of the 2026-11-26 run, from the
// fixture's real times. Mockup only; the built page polls the API instead.

import { useCallback, useEffect, useMemo, useState } from "react";
import raw from "./run-20261126.json";
import type {
  BreakAnswer,
  Demo,
  IncidentDetail,
  IncidentSummary,
  Live,
  Metrics,
  Mode,
  Phase,
  TraceEvent,
} from "../api/contract";
import { ms } from "../lib/format";

interface Checkpoint {
  at: string;
  phase: Phase;
  status: IncidentDetail["status"];
  pr_state: IncidentDetail["pr_state"];
  pr: IncidentDetail["pr"];
  answer: boolean;
  run: IncidentDetail["run"];
  alarm_ok: boolean;
  closed: boolean;
  summary: string;
}

interface Row {
  at: string;
  settled: boolean;
  requests: number;
  errors: number | null;
  latency: number | null;
  pool_wait: number | null;
}

interface Fixture {
  about: string;
  estimated: string[];
  break: { mode: Mode; at: string; fault_ends_at: string };
  incident: IncidentDetail;
  checkpoints: Checkpoint[];
  metrics_rows: Row[];
  variants: { case: string; summary: string }[];
  repeat: { trace: TraceEvent[]; checkpoints: Checkpoint[] };
}

export const F = raw as unknown as Fixture;

export type Scenario =
  | "latest"
  | "live"
  | "busy"
  | "settling"
  | "cooldown"
  | "visitor_day"
  | "daily"
  | "monthly"
  | "paused"
  | "unavailable";

export const SCENARIOS: { id: Scenario; label: string }[] = [
  { id: "latest", label: "Ready: latest run shown" },
  { id: "live", label: "My break, live" },
  { id: "busy", label: "Someone else's break" },
  { id: "settling", label: "Incident settling" },
  { id: "cooldown", label: "My 20-minute cooldown" },
  { id: "visitor_day", label: "My 3 today used" },
  { id: "daily", label: "Day's 20 used" },
  { id: "monthly", label: "Month's 200 used" },
  { id: "paused", label: "Paused" },
  { id: "unavailable", label: "Break fails (unavailable)" },
];

const BREAK = ms(F.break.at);
const OPENED = ms(F.incident.opened_at);
const CLOSED = ms(F.incident.closed_at ?? F.incident.opened_at);
const FAULT_ENDS = ms(F.break.fault_ends_at);
const iso = (t: number) => new Date(t).toISOString();
const shift = (v: string | null, d: number) => (v === null ? null : iso(ms(v) + d));

/** How a run is playing: the fixture's time `t` runs from `startT` at `startWall`, at `speed`. */
interface Play {
  kind: "live" | "replay";
  ending: "recorded" | "repeat"; // a visitor's break ends in the same-fix path; the recorded run as it did
  startWall: number;
  startT: number;
  d: number; // added to every shown time: 0 for the recorded run, "now" for a live one
}

function idAt(t: number): string {
  const s = iso(t).replace(/[-:]/g, "").slice(0, 15);
  return `checkout-api-${s}Z`;
}

function incidentAt(t: number, play: Play | null): IncidentDetail | null {
  const d = play?.d ?? 0;
  const repeat = play?.ending === "repeat";
  const checkpoints = repeat ? F.repeat.checkpoints : F.checkpoints;
  const trace = repeat ? F.repeat.trace : F.incident.trace;
  if (t < OPENED) return null;
  const cp = [...checkpoints].reverse().find((c) => ms(c.at) <= t) ?? checkpoints[0]!;
  const inc = F.incident;
  const alarm = inc.alarms[0]!;
  const clockWas = alarm.at.slice(11, 19);
  const clockNow = iso(ms(alarm.at) + d).slice(11, 19);
  return {
    ...inc,
    id: d === 0 ? inc.id : idAt(OPENED + d),
    opened_at: iso(OPENED + d),
    closed_at: cp.closed ? shift(inc.closed_at, d) : null,
    alarms: [{ ...alarm, at: iso(ms(alarm.at) + d), ok_at: cp.alarm_ok ? shift(alarm.ok_at, d) : null }],
    status: cp.status,
    summary: cp.summary.replace(clockWas, clockNow),
    run: cp.run,
    injected: inc.injected && {
      ...inc.injected,
      at: iso(ms(inc.injected.at) + d),
      fault_ends_at: iso(ms(inc.injected.fault_ends_at) + d),
      runbook_matches: cp.answer ? inc.injected.runbook_matches : null,
    },
    pr_state: cp.pr_state,
    trace: trace.filter((e) => ms(e.at) <= t).map((e) => ({ ...e, at: iso(ms(e.at) + d) })),
    answer: cp.answer ? inc.answer : null,
    pr: cp.pr && { ...cp.pr, as_of: shift(cp.pr.as_of, d) },
  };
}

function metricsAt(t: number, d: number): Metrics | null {
  // A period's metrics land up to 20 s after it starts; the page shows settled ones.
  const landed = F.metrics_rows.filter((r) => ms(r.at) + 18_000 <= t).slice(-18);
  if (landed.length === 0) return null;
  const pick = (k: "errors" | "latency" | "pool_wait") => landed.map((r) => (r.requests ? r[k] : null));
  return {
    period_s: 10,
    from: iso(ms(landed[0]!.at) + d),
    to: iso(ms(landed[landed.length - 1]!.at) + d),
    series: [
      { alarm: "errors", label: "Error rate", unit: "%", threshold: 5, points: pick("errors") },
      { alarm: "latency", label: "Latency p99", unit: "ms", threshold: 1500, points: pick("latency") },
      { alarm: "pool_wait", label: "Pool wait p99", unit: "ms", threshold: 100, points: pick("pool_wait") },
    ],
  };
}

function phaseAt(t: number, play: Play | null): Phase {
  if (!play) return "ready";
  if (t < OPENED) return "fault_active";
  if (t >= CLOSED) return "ready";
  const checkpoints = play.ending === "repeat" ? F.repeat.checkpoints : F.checkpoints;
  return ([...checkpoints].reverse().find((c) => ms(c.at) <= t) ?? checkpoints[0]!).phase;
}

const NEXT_MIDNIGHT = () => {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)).toISOString();
};

function initialPlay(s: Scenario, now: number): Play | null {
  const live = (offset: number): Play => ({
    kind: "live",
    ending: "repeat",
    startWall: now,
    startT: BREAK + offset,
    d: now - BREAK - offset,
  });
  switch (s) {
    case "live":
      return live(0);
    case "busy":
      return live(62_000);
    case "settling":
      return live(200_000);
    case "latest":
    case "unavailable":
      return null;
    default:
      // At a limit the page plays the latest run, from its alarm.
      return { kind: "replay", ending: "recorded", startWall: now, startT: OPENED, d: 0 };
  }
}

function viewerAnswerFor(s: Scenario, now: number): BreakAnswer | null {
  switch (s) {
    case "live":
      return { ok: true, reason: "started", retry_at: null, injected: null };
    case "cooldown":
      return { ok: false, reason: "cooldown", retry_at: new Date(now + 14 * 60_000).toISOString(), injected: null };
    case "visitor_day":
      return { ok: false, reason: "visitor_day", retry_at: NEXT_MIDNIGHT(), injected: null };
    default:
      return null;
  }
}

export interface Run {
  kind: "live" | "replay" | "latest";
  /** Null while a live break waits for its alarm. */
  incident: IncidentDetail | null;
  done: boolean;
}

export interface Mock {
  scenario: Scenario;
  setScenario: (s: Scenario) => void;
  speed: number;
  setSpeed: (n: number) => void;
  showBar: boolean;
  /** What GET /api/live would answer now. */
  live: Live;
  /** What GET /api/incidents would answer now. */
  incidents: IncidentSummary[];
  /** What the run panel shows: the live incident, a replay of the recorded one, or the latest. */
  run: Run;
  /** GET /api/incidents/{id}. */
  detail: (id: string) => IncidentDetail | null;
  /** The viewer's last POST /api/break answer, which the page keeps in the browser. */
  viewer: BreakAnswer | null;
  onBreak: (mode: Mode) => void;
  replay: () => void;
}

const summaryOf = (i: IncidentDetail): IncidentSummary => {
  const { trace: _t, answer: _a, pr: _p, ...summary } = i;
  return summary;
};

export function useMock(): Mock {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const first = (SCENARIOS.find((x) => x.id === params.get("s"))?.id ?? "latest") as Scenario;
  const [scenario, setScenarioState] = useState<Scenario>(first);
  const [speed, setSpeed] = useState<number>(Number(params.get("speed")) || 1);
  const [play, setPlay] = useState<Play | null>(() => initialPlay(first, Date.now()));
  const [viewer, setViewer] = useState<BreakAnswer | null>(() => viewerAnswerFor(first, Date.now()));
  const [now, setNow] = useState(() => Date.now());

  const recordedEnd = ms(F.checkpoints[F.checkpoints.length - 1]!.at);
  const replayEnd = ms(F.incident.pr?.as_of ?? F.checkpoints[3]!.at) + 1_000;
  const end = play?.kind === "replay" ? replayEnd : CLOSED;
  const t = play ? Math.min(play.startT + (now - play.startWall) * speed, end) : recordedEnd;
  const running = play !== null && t < end;

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [running]);

  const setScenario = useCallback((s: Scenario) => {
    const at = Date.now();
    setScenarioState(s);
    setPlay(initialPlay(s, at));
    setViewer(viewerAnswerFor(s, at));
    setNow(at);
  }, []);

  const d = play?.d ?? 0;
  const isLive = play?.kind === "live";
  const blockedGlobal: Demo["blocked"] =
    scenario === "daily" ? "daily" : scenario === "monthly" ? "monthly" : scenario === "paused" ? "paused" : null;
  const phase: Phase = scenario === "paused" ? "paused" : isLive ? phaseAt(t, play) : "ready";
  const busy = isLive && phase !== "ready" && phase !== "paused";
  const demo: Demo = {
    phase,
    blocked: busy ? "busy" : blockedGlobal,
    retry_at: busy ? iso(CLOSED + d) : blockedGlobal === "daily" ? NEXT_MIDNIGHT() : null,
    injected:
      isLive && t < FAULT_ENDS ? { mode: F.break.mode, at: iso(BREAK + d), fault_ends_at: iso(FAULT_ENDS + d) } : null,
    today: { used: scenario === "daily" ? 20 : 6, limit: 20 },
  };

  const recorded = incidentAt(recordedEnd, null)!;
  const liveIncident = isLive ? incidentAt(t, play) : null;
  const metrics = isLive && (phase === "fault_active" || phase === "investigating") ? metricsAt(t, d) : null;
  const live: Live = { v: 1, as_of: iso(t + d), demo, incident: liveIncident ?? recorded, metrics };
  const incidents = [...(liveIncident ? [summaryOf(liveIncident)] : []), summaryOf(recorded)];
  const run: Run =
    play?.kind === "replay"
      ? { kind: "replay", incident: incidentAt(t, play), done: !running }
      : isLive
        ? { kind: "live", incident: liveIncident, done: phase === "ready" }
        : { kind: "latest", incident: recorded, done: true };

  const detail = useCallback(
    (id: string) => {
      if (liveIncident && id === liveIncident.id) return liveIncident;
      if (id === recorded.id) return recorded;
      return null;
    },
    [liveIncident, recorded],
  );

  const onBreak = useCallback(
    (_mode: Mode) => {
      const at = Date.now();
      if (scenario === "unavailable") {
        setViewer({ ok: false, reason: "unavailable", retry_at: null, injected: null });
        return;
      }
      setScenarioState("live");
      setPlay(initialPlay("live", at));
      setViewer(viewerAnswerFor("live", at));
      setNow(at);
    },
    [scenario],
  );

  const replay = useCallback(() => {
    const at = Date.now();
    setPlay({ kind: "replay", ending: "recorded", startWall: at, startT: OPENED, d: 0 });
    setNow(at);
  }, []);

  return {
    scenario,
    setScenario,
    speed,
    setSpeed,
    showBar: params.get("bar") !== "0",
    live,
    incidents,
    run,
    detail,
    viewer,
    onBreak,
    replay,
  };
}
