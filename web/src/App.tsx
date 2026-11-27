import { useState } from "react";
import { BreakPanel } from "./components/BreakPanel";
import { Footer, Header, Intro } from "./components/Frame";
import { RunPanel, type RunKind } from "./components/RunPanel";
import { Timeline } from "./components/Timeline";
import { useMock } from "./mock/player";
import { ReviewBar } from "./mock/ReviewBar";

export function App() {
  const mock = useMock();
  const [selected, setSelected] = useState<string | null>(null);

  const followed = mock.run.incident?.id ?? mock.live.incident?.id ?? null;
  const picked = selected && selected !== followed ? mock.detail(selected) : null;
  const kind: RunKind = picked ? "selected" : mock.run.kind;
  const incident = picked ?? mock.run.incident;

  return (
    <div className="flex min-h-dvh flex-col">
      <Header />
      <Intro />
      <main className="mx-auto grid w-full max-w-[1200px] flex-1 content-start gap-4 px-4 pb-12 md:px-6 lg:grid-cols-[320px_minmax(0,1fr)] lg:gap-6">
        <div className="flex min-w-0 flex-col gap-4 lg:order-2">
          <BreakPanel
            demo={mock.live.demo}
            viewer={mock.viewer}
            onBreak={(mode) => {
              setSelected(null);
              mock.onBreak(mode);
            }}
          />
          <RunPanel
            kind={kind}
            incident={incident}
            demo={mock.live.demo}
            metrics={picked ? null : mock.live.metrics}
            done={mock.run.done}
            onReplay={() => {
              setSelected(null);
              mock.replay();
            }}
            onBackToLive={picked && mock.run.kind === "live" ? () => setSelected(null) : null}
          />
        </div>
        <div className="min-w-0 lg:order-1">
          <Timeline incidents={mock.incidents} selected={incident?.id ?? null} onSelect={setSelected} />
        </div>
      </main>
      <Footer />
      {mock.showBar && <ReviewBar mock={mock} />}
    </div>
  );
}
