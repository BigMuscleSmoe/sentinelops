import type { IncidentSummary } from "../api/contract";
import { dated } from "../lib/format";
import { modeTitle, PR_STATE, type Tone } from "../lib/labels";
import { Badge, Dot } from "./ui";

function rowBadge(i: IncidentSummary): { label: string; tone: Tone; live: boolean } {
  if (i.status === "investigating") return { label: "Investigating", tone: "warn", live: true };
  if (i.status === "halted") return { label: "Halted", tone: "alarm", live: false };
  if (i.status === "refused") return { label: "Not investigated", tone: "plain", live: false };
  if (i.status === "failed") return { label: "Run failed", tone: "alarm", live: false };
  const pr = PR_STATE[i.pr_state];
  return { label: pr.label, tone: pr.tone, live: i.closed_at === null && (i.pr_state === "opening" || i.pr_state === "draft") };
}

export function Timeline({
  incidents,
  selected,
  onSelect,
}: {
  incidents: IncidentSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <aside aria-labelledby="incidents-title" className="min-w-0">
      <div className="rounded-md border border-line bg-panel lg:sticky lg:top-4">
        <div className="flex items-baseline justify-between border-b border-line px-4 py-3">
          <h2 id="incidents-title" className="text-[15px] font-semibold tracking-tight">
            Incidents
          </h2>
          <span className="text-[13px] text-muted">newest first</span>
        </div>
        <ol>
          {incidents.map((i) => {
            const b = rowBadge(i);
            const current = i.id === selected;
            return (
              <li key={i.id} className="border-b border-line last:border-b-0">
                <button
                  type="button"
                  onClick={() => onSelect(i.id)}
                  aria-current={current ? "true" : undefined}
                  className={`block w-full border-l-2 px-4 py-3 text-left hover:bg-sunk ${
                    current ? "border-l-ink bg-sunk" : "border-l-transparent"
                  }`}
                >
                  <span className="flex items-center justify-between gap-2 text-[12.5px] text-muted">
                    <time dateTime={i.opened_at} className="flex items-center gap-2">
                      {b.live && <Dot tone="warn" />}
                      {dated(i.opened_at)}
                    </time>
                    <Badge tone={b.tone}>{b.label}</Badge>
                  </span>
                  <span className="mt-1.5 block text-[14px] leading-snug">{i.summary}</span>
                  {i.injected && (
                    <span className="mt-1.5 block text-[12.5px] text-muted">
                      Demo break: {modeTitle(i.injected.mode).toLowerCase()}
                      {i.injected.runbook_matches === true && ", and the cited runbook is its own"}
                      {i.injected.runbook_matches === false && ", but the cited runbook isn't its own"}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ol>
        <p className="border-t border-line px-4 py-2.5 text-[12.5px] leading-snug text-muted">
          Each line is built in code from the incident's typed fields; no model writes it.
        </p>
      </div>
    </aside>
  );
}
