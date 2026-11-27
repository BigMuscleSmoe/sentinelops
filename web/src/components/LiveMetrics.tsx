import type { Metrics, Series } from "../api/contract";
import { clock } from "../lib/format";

function show(v: number, unit: Series["unit"]): string {
  if (unit === "%") return `${v.toFixed(1)}%`;
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

function Bars({ s }: { s: Series }) {
  const top = Math.max(s.threshold * 1.8, ...s.points.map((p) => p ?? 0));
  const w = 180;
  const h = 44;
  const gap = 2;
  const bw = (w - gap * (s.points.length - 1)) / Math.max(s.points.length, 1);
  const y = (v: number) => h - (v / top) * (h - 4);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-11 w-full" aria-hidden="true">
      {s.points.map((p, i) =>
        p === null ? null : (
          <rect
            key={i}
            x={i * (bw + gap)}
            y={y(p)}
            width={bw}
            height={Math.max(h - y(p), 1)}
            fill={p > s.threshold ? "var(--color-alarm)" : "var(--color-line-strong)"}
          />
        ),
      )}
      <line x1="0" x2={w} y1={y(s.threshold)} y2={y(s.threshold)} stroke="var(--color-ink-2)" strokeDasharray="3 3" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function LiveMetrics({ metrics }: { metrics: Metrics }) {
  return (
    <div className="border-t border-line px-4 py-3">
      <p className="text-[12.5px] text-muted">
        checkout-api's metrics, 10 s periods, {clock(metrics.from)} to {clock(metrics.to)} UTC. Dashed: each alarm's
        threshold; an alarm fires when 2 of 3 periods cross it.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        {metrics.series.map((s) => {
          const last = [...s.points].reverse().find((p): p is number => p !== null) ?? null;
          const over = last !== null && last > s.threshold;
          return (
            <div key={s.alarm} className="min-w-0 rounded border border-line px-3 py-2.5">
              <div className="flex items-baseline justify-between gap-2">
                <p className="text-[13px] font-medium">{s.label}</p>
                <p className={`font-mono text-[15px] font-semibold ${over ? "text-alarm" : "text-ink"}`}>
                  {last === null ? "-" : show(last, s.unit)}
                </p>
              </div>
              <Bars s={s} />
              <p className="text-[12px] text-muted">alarm over {show(s.threshold, s.unit)}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
