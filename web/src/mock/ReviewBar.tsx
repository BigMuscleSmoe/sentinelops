// Mockup only: switches between the page's states for review. Not part of the built page.

import { useState } from "react";
import { F, SCENARIOS, type Mock } from "./player";

export function ReviewBar({ mock }: { mock: Mock }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="fixed right-3 bottom-3 z-50 flex max-w-[calc(100vw-24px)] flex-col items-end gap-2">
      {open && (
        <div
          id="review-panel"
          className="max-h-[70dvh] w-[360px] max-w-full overflow-auto rounded-md border border-ink/80 bg-ink p-4 text-[13px] text-white shadow-lg"
        >
          <p className="font-semibold">Mockup controls</p>
          <p className="mt-1 text-white/70">
            Data from the 2026-11-26 run; nothing calls AWS. Pick a state; "My break" plays the run at the chosen speed.
          </p>
          <fieldset className="mt-3">
            <legend className="text-[12px] tracking-wide text-white/60 uppercase">State</legend>
            <div className="mt-1.5 grid grid-cols-2 gap-1.5">
              {SCENARIOS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  aria-pressed={mock.scenario === s.id}
                  onClick={() => mock.setScenario(s.id)}
                  className={`min-h-9 rounded border px-2 py-1 text-left text-[12.5px] leading-tight ${
                    mock.scenario === s.id ? "border-white bg-white text-ink" : "border-white/25 hover:border-white/60"
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </fieldset>
          <fieldset className="mt-3">
            <legend className="text-[12px] tracking-wide text-white/60 uppercase">Speed</legend>
            <div className="mt-1.5 flex gap-1.5">
              {[1, 4, 16].map((n) => (
                <button
                  key={n}
                  type="button"
                  aria-pressed={mock.speed === n}
                  onClick={() => mock.setSpeed(n)}
                  className={`h-9 w-14 rounded border ${mock.speed === n ? "border-white bg-white text-ink" : "border-white/25 hover:border-white/60"}`}
                >
                  {n}x
                </button>
              ))}
            </div>
          </fieldset>
          <div className="mt-3">
            <p className="text-[12px] tracking-wide text-white/60 uppercase">The summary line's other outcomes (illustrative)</p>
            <ul className="mt-1.5 space-y-1.5">
              {F.variants.map((v) => (
                <li key={v.case}>
                  <span className="text-white/60">{v.case}:</span> {v.summary}
                </li>
              ))}
            </ul>
          </div>
          <div className="mt-3">
            <p className="text-[12px] tracking-wide text-white/60 uppercase">Estimated, not logged</p>
            <ul className="mt-1.5 list-disc space-y-1 pl-4 text-white/80">
              {F.estimated.map((e) => (
                <li key={e}>{e}</li>
              ))}
              <li>A visitor's break replays the 11-26 agent run; its ending is the flow's same-fix path, since PR #2 stays open.</li>
              <li>Live metrics are the agent's own 10 s table from that run, so they stop at 18:36:30.</li>
            </ul>
          </div>
        </div>
      )}
      <button
        type="button"
        aria-expanded={open}
        aria-controls="review-panel"
        onClick={() => setOpen((o) => !o)}
        className="h-11 rounded-md border border-ink bg-ink px-4 text-[13px] font-semibold text-white shadow-md"
      >
        {open ? "Close" : "Mockup"}
      </button>
    </div>
  );
}
