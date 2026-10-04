"use client";

import { useRef, useState } from "react";

export type Series = { key: string; label: string; color: string; dash?: string; points: { t: number; v: number | null }[] };

const fmtHM = (t: number) =>
  new Date(t).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });

/** A day on an IST axis: several labelled lines, reference lines, and a read-out on hover. */
export default function DayChart({
  series, t0, t1, yMin, yMax, yStep, unit, refs = [], height = 220, label,
}: {
  series: Series[]; t0: number; t1: number; yMin: number; yMax: number; yStep: number; unit: string;
  refs?: { v: number; label: string }[]; height?: number; label: string;
}) {
  const [hoverT, setHoverT] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const W = 960, PL = 48, PR = 16, TOP = 24, AX = 22;
  const H = height, MAIN = H - TOP - AX;
  const x = (t: number) => PL + ((t - t0) / (t1 - t0)) * (W - PL - PR);
  const y = (v: number) => TOP + MAIN - ((Math.min(yMax, Math.max(yMin, v)) - yMin) / (yMax - yMin)) * MAIN;

  const ticks: number[] = [];
  for (let t = Math.ceil(t0 / 3_600_000) * 3_600_000; t <= t1; t += 3_600_000) {
    const h = Number(new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", hour12: false }));
    if (h % 3 === 0) ticks.push(t);
  }
  const yTicks: number[] = [];
  for (let v = Math.ceil(yMin / yStep) * yStep; v <= yMax + 1e-9; v += yStep) yTicks.push(v);

  const path = (s: Series) => {
    let d = "", pen = false;
    for (const p of s.points) {
      if (p.v == null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
      pen = true;
    }
    return d;
  };

  const nearest = (s: Series, t: number) => {
    let best: { t: number; v: number | null } | null = null;
    for (const p of s.points) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    return best;
  };

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((e.clientX - box.left) / box.width) * W;
    setHoverT(px < PL || px > W - PR ? null : t0 + ((px - PL) / (W - PL - PR)) * (t1 - t0));
  }

  const readings = hoverT != null ? series.map((s) => ({ s, p: nearest(s, hoverT) })) : null;

  return (
    <figure>
      <figcaption className="mb-1 flex min-h-[1.5rem] flex-wrap items-baseline gap-x-4 text-[13px] text-[var(--leo-text-dim)]">
        {readings && readings[0].p ? (
          <>
            <span className="text-[var(--leo-text)]">{fmtHM(readings[0].p.t)} IST</span>
            {readings.map(({ s, p }) => (
              <span key={s.key}>{s.label} <span className="text-[var(--leo-text)]">{p?.v != null ? `${p.v.toFixed(unit === "%" ? 0 : 1)} ${unit}` : "no data"}</span></span>
            ))}
          </>
        ) : (
          <>
            {series.map((s) => (
              <span key={s.key} className="flex items-center gap-1.5">
                <svg width="20" height="8" aria-hidden><line x1="0" x2="20" y1="4" y2="4" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dash} /></svg>
                {s.label}
              </span>
            ))}
            <span>Point at the chart to read values.</span>
          </>
        )}
      </figcaption>
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label={label}
        onMouseMove={onMove} onMouseLeave={() => setHoverT(null)}>
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PL} x2={W - PR} y1={y(v)} y2={y(v)} stroke="#26323f" strokeWidth={0.6} />
            <text x={PL - 8} y={y(v) + 4} fontSize={12} textAnchor="end" fill="#93a1b0">{v}</text>
          </g>
        ))}
        <text x={PL - 8} y={12} fontSize={12} textAnchor="end" fill="#93a1b0">{unit}</text>
        {refs.map((r) => (
          <g key={r.label}>
            <line x1={PL} x2={W - PR} y1={y(r.v)} y2={y(r.v)} stroke="#e0473e" strokeWidth={1.2} strokeDasharray="6 4" />
            <text x={W - PR - 4} y={y(r.v) - 5} fontSize={12} textAnchor="end" fill="#ff8a82">{r.label}</text>
          </g>
        ))}
        {series.map((s) => (
          <path key={s.key} d={path(s)} fill="none" stroke={s.color} strokeWidth={2} strokeDasharray={s.dash} strokeLinejoin="round" />
        ))}
        {ticks.map((t) => (
          <text key={t} x={x(t)} y={H - 4} fontSize={12} textAnchor="middle" fill="#93a1b0">{fmtHM(t)}</text>
        ))}
        {hoverT != null && <line x1={x(hoverT)} x2={x(hoverT)} y1={TOP} y2={TOP + MAIN} stroke="#7cc4ff" />}
      </svg>
    </figure>
  );
}
