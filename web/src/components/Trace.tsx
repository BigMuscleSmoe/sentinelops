import type { ReactNode } from "react";
import type { IncidentDetail, TraceEvent } from "../api/contract";
import { clock, int, usd } from "../lib/format";
import { TOOLS } from "../lib/labels";
import { Badge, Disclosure, Prose } from "./ui";

type Of<K extends TraceEvent["kind"]> = Extract<TraceEvent, { kind: K }>;

const ALARM_LABEL = { errors: "Errors over 5%", latency: "Latency p99 over 1.5 s", pool_wait: "Pool wait p99 over 100 ms" };

function args(a: Of<"tool_call">["args"]): string {
  return Object.entries(a)
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`)
    .join(", ");
}

function Row({ at, tag, fresh, children }: { at: string; tag: ReactNode; fresh: boolean; children: ReactNode }) {
  return (
    <li className={`grid grid-cols-[52px_minmax(0,1fr)] gap-x-3 px-4 py-2.5 sm:grid-cols-[64px_minmax(0,1fr)] ${fresh ? "arrive" : ""}`}>
      <time dateTime={at} className="pt-px font-mono text-[12px] text-muted">
        {clock(at)}
      </time>
      <div className="min-w-0">
        <div className="mb-1">{tag}</div>
        {children}
      </div>
    </li>
  );
}

const Tag = ({ children }: { children: ReactNode }) => (
  <span className="text-[11.5px] font-medium tracking-wide text-muted uppercase">{children}</span>
);

function ToolRow({ call, result, fresh }: { call: Of<"tool_call">; result: Of<"tool_result"> | undefined; fresh: boolean }) {
  const preview = result?.text.split("\n").find((l) => l.trim()) ?? "";
  return (
    <Row
      at={call.at}
      fresh={fresh}
      tag={
        <Tag>
          {TOOLS[call.tool]}
          {result?.source && <span className="ml-2 font-mono normal-case tracking-normal">[{result.source}]</span>}
        </Tag>
      }
    >
      <p className="font-mono text-[13px] leading-snug break-words">
        <span className="font-semibold">{call.tool}</span>
        <span className="text-ink-2">({args(call.args)})</span>
      </p>
      {result ? (
        <div className="mt-1">
          <p className="line-clamp-2 text-[13px] leading-snug text-muted">{preview}</p>
          <Disclosure
            label={`Show the result: ${int(result.chars)} characters${result.truncated ? `, the first ${int(result.text.length)} here` : ""}`}
          >
            <pre
              tabIndex={0}
              aria-label={`Result of ${call.tool}`}
              className="mt-1 max-h-80 overflow-auto rounded border border-line bg-sunk p-3 font-mono text-[12px] leading-normal whitespace-pre text-ink-2"
            >
              {result.text}
            </pre>
          </Disclosure>
        </div>
      ) : (
        <p className="mt-1 flex items-center gap-2 text-[13px] text-warn">
          <span className="pulse-ring inline-block size-2 rounded-full bg-warn" aria-hidden="true" />
          Running
        </p>
      )}
    </Row>
  );
}

export function Trace({ incident, live }: { incident: IncidentDetail; live: boolean }) {
  const events = incident.trace;
  const results = new Map(events.flatMap((e) => (e.kind === "tool_result" ? [[e.call, e] as const] : [])));
  const newest = events.length ? events[events.length - 1]!.seq : 0;
  const tools = events.filter((e) => e.kind === "tool_call").length;
  const calls = events.filter((e): e is Of<"model_call"> => e.kind === "model_call");
  const cost = calls.reduce((s, c) => s + c.cost_usd, 0);
  const fresh = (e: TraceEvent) => live && e.seq >= newest - 1;
  const pr = incident.pr;

  const items = events.map((e): ReactNode => {
    switch (e.kind) {
      case "tool_result":
        return null;
      case "tool_call":
        return <ToolRow key={e.seq} call={e} result={results.get(e.call)} fresh={fresh(e)} />;
      case "run_started":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Run started</Tag>}>
            <p className="text-[14px] leading-snug">
              On the alarm: {ALARM_LABEL[e.alarm]}. Limits, enforced in code: 15 tool calls, 200k input tokens, $0.10,
              300 s.
            </p>
          </Row>
        );
      case "model_call":
        return (
          <li key={e.seq} className="flex items-center gap-2 px-4 pt-3 pb-1 text-[12px] text-muted">
            <span className="font-medium text-ink-2">Model call {e.n}</span>
            <span className="h-px flex-1 bg-line" aria-hidden="true" />
            <span className="font-mono">
              {int(e.input_tokens)} in, {int(e.output_tokens)} out, {usd(e.cost_usd)}
            </span>
          </li>
        );
      case "thinking":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Thinking, summarized</Tag>}>
            <p className="border-l-2 border-line-strong pl-3 text-[14px] leading-relaxed whitespace-pre-line text-ink-2">
              <Prose text={e.text} />
            </p>
          </Row>
        );
      case "said":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Agent</Tag>}>
            <p className="text-[15px] leading-relaxed">
              <Prose text={e.text} />
            </p>
          </Row>
        );
      case "check":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Check, in code</Tag>}>
            <p className="flex flex-wrap items-baseline gap-2 text-[14px] leading-snug">
              <Badge tone={e.kept ? "ok" : "alarm"}>
                {e.check === "citation" ? "Citation" : "Patch"} {e.kept ? "kept" : "dropped"}
              </Badge>
              <span>{e.detail}</span>
            </p>
          </Row>
        );
      case "answered":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Answer</Tag>}>
            <p className="text-[14px] leading-snug">
              Answered with confidence {e.confidence.toFixed(2)}.{" "}
              <a href="#diagnosis" className="font-medium text-link hover:underline">
                The diagnosis
              </a>
            </p>
          </Row>
        );
      case "halted":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Halted</Tag>}>
            <p className="text-[14px]">Stopped at the {e.by.replace("_", " ")} limit, enforced in code.</p>
          </Row>
        );
      case "handed_off":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">Handed to the remediation flow, which has no model in it.</p>
          </Row>
        );
      case "pr_opened":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">
              Opened draft{" "}
              <a href="#pr" className="font-medium text-link hover:underline">
                PR #{e.number}
              </a>{" "}
              as the GitHub App, then waited on CI.
            </p>
          </Row>
        );
      case "pr_same_fix":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">
              The same fix is already open as{" "}
              <a href="#pr" className="font-medium text-link hover:underline">
                PR #{e.number}
              </a>
              , so nothing new was opened{e.commented ? "; it noted this incident in a comment" : "; today's comment was already made"}.
            </p>
          </Row>
        );
      case "pr_checks":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>CI</Tag>}>
            <p className="flex flex-wrap items-baseline gap-2 text-[14px] leading-snug">
              <Badge tone={e.state === "success" ? "ok" : e.state === "failure" ? "alarm" : "warn"}>
                {e.state === "success" ? "Passed" : e.state === "failure" ? "Failed" : "Running"}
              </Badge>
              <span className="font-mono text-[13px]">{e.check}</span>
            </p>
          </Row>
        );
      case "pr_ready":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">Marked PR #{e.number} ready for review. A person merges it, or doesn't.</p>
          </Row>
        );
      case "pr_rejected":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">The flow's own check refused the patch ({e.check}); no PR.</p>
          </Row>
        );
      case "flow_failed":
        return (
          <Row key={e.seq} at={e.at} fresh={fresh(e)} tag={<Tag>Remediation flow</Tag>}>
            <p className="text-[14px] leading-snug">The flow failed ({e.error}); no PR.</p>
          </Row>
        );
    }
  });

  return (
    <section aria-labelledby="trace-title" className="min-w-0 rounded-md border border-line bg-panel">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-3">
        <h2 id="trace-title" className="text-[15px] font-semibold tracking-tight">
          What the agent did
        </h2>
        <p className="font-mono text-[12.5px] text-muted">
          {tools} tool calls, {calls.length} model calls, {usd(cost)}
        </p>
      </div>
      <ol className="divide-y divide-line/70 pb-1" aria-live={live ? "polite" : undefined} aria-relevant="additions">
        {items}
      </ol>
      {pr && pr.state !== "opening" && (
        <p className="border-t border-line px-4 py-2.5 text-[12.5px] text-muted">
          Times are UTC. Summarized thinking is the model API's own summary of its reasoning.
        </p>
      )}
    </section>
  );
}
