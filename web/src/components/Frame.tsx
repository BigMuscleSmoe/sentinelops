import { ExternalLink } from "./ui";

const REPO = "https://github.com/BigMuscleSmoe/sentinelops";

function Mark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" className="size-5">
      <rect width="20" height="20" rx="4" fill="var(--color-ink)" />
      <path
        d="M3.5 11h3l1.6-4.2 2.6 7.4 1.8-4.6.9 1.4h3.1"
        fill="none"
        stroke="#fff"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Header() {
  return (
    <header className="border-b border-line bg-panel">
      <div className="mx-auto flex h-14 max-w-[1200px] items-center justify-between gap-4 px-4 md:px-6">
        <a href="/" className="flex items-center gap-2 text-[15px] font-semibold tracking-tight">
          <Mark />
          SentinelOps
        </a>
        <nav aria-label="Project" className="flex items-center gap-4 text-[14px]">
          <a href={`${REPO}#readme`} className="text-ink-2 hover:text-ink hover:underline">
            How it works
          </a>
          <a href={REPO} className="text-ink-2 hover:text-ink hover:underline">
            Source
          </a>
        </nav>
      </div>
    </header>
  );
}

const FACTS: [string, string][] = [
  ["Agent", "Claude Haiku 5.5, on AgentCore Runtime"],
  ["Tools", "5, all read-only"],
  ["Limits, in code", "15 tool calls, $0.10 a run"],
  ["Fix PRs", "Opened by a bot that can't merge"],
];

export function Intro() {
  return (
    <section className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-5 md:px-6 md:pt-10 md:pb-7">
      <h1 className="max-w-[30ch] text-[26px] leading-[1.15] font-semibold tracking-tight md:text-[34px]">
        Break a checkout service. Watch an AI agent find out why.
      </h1>
      <p className="mt-3 max-w-[70ch] text-[15px] leading-relaxed text-ink-2 md:text-[16px]">
        SentinelOps is an incident response agent on AWS. When an alarm fires, it reads the service's metrics, logs,
        deploys, runbooks, and code. The agent reads and reasons but can't change anything. When it finds a bug in the
        service's own code, a separate flow with no model in it opens a draft fix PR that only a person can merge.
      </p>
      <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-line pt-4 sm:grid-cols-4">
        {FACTS.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-[12px] font-medium tracking-wide text-muted uppercase">{k}</dt>
            <dd className="mt-0.5 text-[14px] leading-snug">{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex max-w-[1200px] flex-col gap-2 px-4 py-6 text-[13px] text-muted md:flex-row md:items-center md:justify-between md:px-6">
        <p>SentinelOps, a portfolio project. No cookies, no analytics, no third-party scripts.</p>
        <p>
          Source, design, and costs: <ExternalLink href={REPO}>github.com/BigMuscleSmoe/sentinelops</ExternalLink>
        </p>
      </div>
    </footer>
  );
}
