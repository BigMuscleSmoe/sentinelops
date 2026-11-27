import type { Answer as AnswerT, Evidence, IncidentDetail } from "../api/contract";
import { modeTitle, WHERE } from "../lib/labels";
import { Badge, Card, CardHead, Disclosure, Prose } from "./ui";

const ISO = /^\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)/;
const t = (v: string | null) => (v === null ? "" : (ISO.exec(v)?.[1] ?? v));

/** One line per evidence item, from its typed fields. */
function cite(e: Evidence): string {
  const lines = e.lines ? ` (lines ${e.lines.length > 4 ? `${e.lines.slice(0, 3).join(", ")} and ${e.lines.length - 3} more` : e.lines.join(", ")})` : "";
  switch (e.kind) {
    case "value":
      return `${e.column} at ${t(e.at)} = ${e.value}${lines}`;
    case "range":
      return `${e.column} ${t(e.start)} to ${t(e.end)}: ${e.low} to ${e.high}${lines}`;
    case "count":
      return `${e.count} lines with ${e.column} = ${e.value}${lines}`;
    default:
      return `${e.kind} ${e.column ?? ""} ${e.value ?? ""}${lines}`.trim();
  }
}

export function Answer({ answer, incident }: { answer: AnswerT; incident: IncidentDetail }) {
  const injected = incident.injected;
  return (
    <Card id="diagnosis">
      <CardHead
        title="Diagnosis"
        aside={
          <>
            <Badge tone={answer.fault_location === "this_service" ? "warn" : "plain"}>{WHERE[answer.fault_location]}</Badge>
            <span className="font-mono">confidence {answer.confidence.toFixed(2)}</span>
          </>
        }
      />
      <div className="space-y-4 px-4 py-4">
        <p className="text-[15px] leading-relaxed">
          <Prose text={answer.root_cause} />
        </p>

        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-[14px] sm:grid-cols-[max-content_minmax(0,1fr)]">
          <dt className="text-muted">Affected</dt>
          <dd className="-mt-1.5 sm:mt-0">{answer.affected_component}</dd>
          <dt className="text-muted">Running version</dt>
          <dd className="-mt-1.5 font-mono text-[13px] sm:mt-0">{answer.running_version ?? "unknown"}</dd>
          {answer.runbook && (
            <>
              <dt className="text-muted">Cited runbook</dt>
              <dd className="-mt-1.5 sm:mt-0">
                <span className="font-mono text-[13px]">{answer.runbook.file}</span>, {answer.runbook.title}
              </dd>
            </>
          )}
        </dl>

        {injected && (
          <p
            className={`rounded border px-3 py-2 text-[13.5px] leading-snug ${
              injected.runbook_matches ? "border-ok/25 bg-ok-soft text-ok" : "border-warn/25 bg-warn-soft text-warn"
            }`}
          >
            Injected by the demo: {modeTitle(injected.mode).toLowerCase()}.{" "}
            {injected.runbook_matches
              ? "The runbook the agent cited is this fault's own. It never sees what was injected."
              : "The runbook the agent cited isn't this fault's own."}
          </p>
        )}

        <div>
          <h3 className="text-[13px] font-semibold">How it got there</h3>
          <ol className="mt-2 space-y-2.5">
            {answer.inferences.map((inf, n) => (
              <li key={n} className="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2 text-[14px] leading-relaxed">
                <span className="font-mono text-[12px] leading-[1.7] text-muted">{n + 1}.</span>
                <div>
                  <p>
                    <Prose text={inf.claim} />
                  </p>
                  <p className="mt-0.5 font-mono text-[12px] text-muted">rests on {inf.rests_on.join(", ")}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>

        <div className="space-y-1 border-t border-line pt-3">
          <Disclosure label={`The ${answer.evidence.length} evidence items, each cited to a tool result`}>
            <ol className="mt-2 space-y-2">
              {answer.evidence.map((e) => (
                <li key={e.id} className="grid grid-cols-[34px_minmax(0,1fr)] gap-x-2 text-[13px] leading-snug">
                  <span className="font-mono text-muted">{e.id}</span>
                  <div className="min-w-0">
                    <p className="font-mono text-[12.5px] break-words">
                      [{e.source}] {cite(e)}
                    </p>
                    <p className="text-ink-2">{e.note}</p>
                  </div>
                </li>
              ))}
            </ol>
          </Disclosure>
          <Disclosure label={`What it didn't check (${answer.not_checked.length})`}>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-[13.5px] leading-snug text-ink-2">
              {answer.not_checked.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </Disclosure>
        </div>
      </div>
    </Card>
  );
}
