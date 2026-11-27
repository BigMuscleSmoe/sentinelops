import type { Demo, IncidentDetail, Metrics } from "../api/contract";
import { clock, dated } from "../lib/format";
import { modeTitle } from "../lib/labels";
import { Answer } from "./Answer";
import { LiveMetrics } from "./LiveMetrics";
import { Pipeline } from "./Pipeline";
import { PrCard } from "./PrCard";
import { Trace } from "./Trace";
import { Badge, Card } from "./ui";

export type RunKind = "live" | "replay" | "latest" | "selected";

interface Props {
  kind: RunKind;
  incident: IncidentDetail | null;
  demo: Demo;
  metrics: Metrics | null;
  done: boolean;
  onReplay: () => void;
  onBackToLive: (() => void) | null;
}

function title(kind: RunKind, incident: IncidentDetail | null, done: boolean): { text: string; badge: [string, "warn" | "plain" | "ok"] | null } {
  switch (kind) {
    case "live":
      return { text: "Live run", badge: done ? ["Finished", "ok"] : ["Live", "warn"] };
    case "replay":
      return { text: incident ? `Replay of the run on ${dated(incident.opened_at)}` : "Replay", badge: done ? ["Replayed", "plain"] : ["Replaying", "plain"] };
    case "latest":
      return { text: "Latest run", badge: null };
    case "selected":
      return { text: incident ? `The run on ${dated(incident.opened_at)}` : "Run", badge: null };
  }
}

export function RunPanel({ kind, incident, demo, metrics, done, onReplay, onBackToLive }: Props) {
  const head = title(kind, incident, done);
  const canReplay = kind === "latest" || (kind === "replay" && done) || kind === "selected";
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-[15px] font-semibold tracking-tight">
              {head.text}
              {head.badge && <Badge tone={head.badge[1]}>{head.badge[0]}</Badge>}
            </h2>
            {incident && <p className="mt-0.5 truncate font-mono text-[12px] text-muted">incident {incident.id}</p>}
          </div>
          <div className="flex items-center gap-2">
            {onBackToLive && (
              <button type="button" onClick={onBackToLive} className="h-9 rounded-md border border-line px-3 text-[13px] font-medium hover:border-line-strong">
                Back to the live run
              </button>
            )}
            {canReplay && (
              <button type="button" onClick={onReplay} className="h-9 rounded-md border border-line px-3 text-[13px] font-medium hover:border-line-strong">
                {kind === "replay" ? "Replay again" : "Replay it"}
              </button>
            )}
          </div>
        </div>
        {!incident && demo.injected && (
          <p className="px-4 pt-3 text-[14px] leading-snug text-ink-2">
            {modeTitle(demo.injected.mode)} injected at {clock(demo.injected.at)} UTC. The alarms check every 10 seconds
            and fire when 2 of 3 periods cross their threshold; the agent starts the moment one does.
          </p>
        )}
        <Pipeline incident={incident} demo={demo} />
        {metrics && <LiveMetrics metrics={metrics} />}
      </Card>
      {incident && incident.trace.length > 0 && <Trace incident={incident} live={kind === "live" || kind === "replay"} />}
      {incident?.answer && <Answer answer={incident.answer} incident={incident} />}
      {incident?.pr && <PrCard pr={incident.pr} />}
    </div>
  );
}
