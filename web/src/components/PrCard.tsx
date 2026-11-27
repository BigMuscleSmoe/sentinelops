import { REPO_PR_URL, type Pr } from "../api/contract";
import { clock, dated } from "../lib/format";
import { BLOCKERS, PR_STATE } from "../lib/labels";
import { Badge, Card, CardHead, ExternalLink, Prose } from "./ui";

function Diff({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <div
      tabIndex={0}
      aria-label="The change, as a unified diff"
      className="overflow-x-auto rounded border border-line bg-sunk py-2 font-mono text-[12.5px] leading-[1.55]"
    >
      {lines.map((l, i) => {
        const kind = l.startsWith("+++") || l.startsWith("---") ? "head" : l.startsWith("@@") ? "hunk" : l[0];
        const cls =
          kind === "+"
            ? "bg-add text-add-ink"
            : kind === "-"
              ? "bg-del text-del-ink"
              : kind === "hunk" || kind === "head"
                ? "text-muted"
                : "text-ink-2";
        return (
          <div key={i} className={`px-3 whitespace-pre ${cls}`}>
            {l || " "}
          </div>
        );
      })}
    </div>
  );
}

export function PrCard({ pr }: { pr: Pr }) {
  const state = PR_STATE[pr.state];
  const url = pr.url && REPO_PR_URL.test(pr.url) ? pr.url : null;
  const conclusion = pr.check?.conclusion;

  return (
    <Card id="pr">
      <CardHead
        title={pr.number ? `Fix PR #${pr.number}` : "Fix PR"}
        aside={<Badge tone={state.tone}>{state.label}</Badge>}
      />
      <div className="space-y-4 px-4 py-4">
        {pr.state === "none" && (
          <div className="text-[14px] leading-relaxed">
            <p className="font-medium">No PR for this incident.</p>
            <ul className="mt-1 list-disc pl-5 text-ink-2">
              {pr.blockers.map((b) => (
                <li key={b}>{BLOCKERS[b]}</li>
              ))}
            </ul>
          </div>
        )}
        {pr.state === "opening" && (
          <p className="text-[14px] text-ink-2">The flow has the answer and is preparing a branch and a draft PR.</p>
        )}
        {pr.title && (
          <>
            {pr.state === "same_fix_open" && (
              <p className="rounded border border-line bg-sunk px-3 py-2 text-[13.5px] leading-snug text-ink-2">
                The same fix was already open, so the flow opened nothing new and noted this incident on PR #{pr.number}.
                It opens one PR per release and file, never a twin.
              </p>
            )}
            <p className="text-[15px] leading-snug font-medium break-words">{pr.title}</p>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-[14px] sm:grid-cols-[max-content_minmax(0,1fr)]">
              <dt className="text-muted">Branch</dt>
              <dd className="-mt-1.5 min-w-0 break-words sm:mt-0">
                <span className="font-mono text-[13px]">{pr.branch}</span> into{" "}
                <span className="font-mono text-[13px]">{pr.base}</span>
              </dd>
              {pr.check && (
                <>
                  <dt className="text-muted">CI</dt>
                  <dd className="-mt-1.5 flex flex-wrap items-center gap-2 sm:mt-0">
                    <span className="font-mono text-[13px]">{pr.check.name}</span>
                    <Badge tone={conclusion === "success" ? "ok" : conclusion === "failure" ? "alarm" : "warn"}>
                      {conclusion === "success" ? "passed" : conclusion === "failure" ? "failed" : "running"}
                    </Badge>
                  </dd>
                </>
              )}
              <dt className="text-muted">Change</dt>
              <dd className="-mt-1.5 min-w-0 break-words sm:mt-0">
                <span className="font-mono text-[13px]">{pr.file}</span>{" "}
                <span className="font-mono text-[13px] text-add-ink">+{pr.additions}</span>{" "}
                <span className="font-mono text-[13px] text-del-ink">-{pr.deletions}</span>
              </dd>
              <dt className="text-muted">Opened by</dt>
              <dd className="-mt-1.5 sm:mt-0">A GitHub App that can push branches and open PRs but can't merge</dd>
            </dl>
            {pr.diff && <Diff diff={pr.diff} />}
            {pr.rationale && (
              <div>
                <h3 className="text-[13px] font-semibold">Why this fix, in the agent's words</h3>
                <p className="mt-1 text-[14px] leading-relaxed text-ink-2">
                  <Prose text={pr.rationale} />
                </p>
              </div>
            )}
          </>
        )}
      </div>
      <div className="flex flex-col gap-2 border-t border-line px-4 py-3 text-[12.5px] leading-snug text-muted sm:flex-row sm:items-center sm:justify-between">
        <p>
          A person reviews and merges it; nothing deploys by itself.
          {pr.as_of && ` As of the flow's finish, ${dated(pr.as_of).replace(/ \d\d:\d\d UTC$/, "")} ${clock(pr.as_of)} UTC.`}
        </p>
        {url && <ExternalLink href={url}>Open PR #{pr.number} on GitHub</ExternalLink>}
      </div>
    </Card>
  );
}
