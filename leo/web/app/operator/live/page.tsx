"use client";

import { useEffect, useState } from "react";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const RUN_ID = "normal";

type DispatchRow = { block_id: string; ts_end: string; setpoint_kw: number; actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null };
type SensorRow = { sensor_id: string; ts_end: string; voltage_v: number; supply_present: boolean };

function Sparkline({ points, color, min, max }: { points: number[]; color: string; min: number; max: number }) {
  const range = Math.max(1e-6, max - min);
  return (
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" className="w-full h-8">
      <polyline
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        points={points
          .map((v, i) => `${(i / Math.max(1, points.length - 1)) * 100},${32 - ((v - min) / range) * 32}`)
          .join(" ")}
      />
    </svg>
  );
}

export default function LiveOperations() {
  const [dispatch, setDispatch] = useState<DispatchRow[]>([]);
  const [sensors, setSensors] = useState<SensorRow[]>([]);

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/dispatch/${RUN_ID}`).then((r) => r.json()).then(setDispatch).catch(() => {});
    fetch(`${CLOUD_API_URL}/api/sensor_readings/${RUN_ID}`).then((r) => r.json()).then(setSensors).catch(() => {});
  }, []);

  const blocks = Array.from(new Set(dispatch.map((d) => d.block_id)));
  const sensorIds = Array.from(new Set(sensors.map((s) => s.sensor_id))).filter((id) => id.includes("FAREND"));
  const ruleTriggers = dispatch.filter((d) => d.rule_triggered);

  return (
    <main id="main-content" className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold">Live operations</h1>
      <p className="text-sm text-[var(--leo-text-dim)] mb-6">
        Per-phase far-end voltage, SoC across the three blocks, current mode, live-rule triggers —
        read directly from what the simulated gateway recorded, not recomputed here.
      </p>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3">Battery SoC by phase</h2>
        <div className="grid grid-cols-3 gap-3">
          {blocks.map((blockId) => {
            const rows = dispatch.filter((d) => d.block_id === blockId);
            const soc = rows.map((r) => r.soc_after);
            const last = rows[rows.length - 1];
            return (
              <div key={blockId} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-3">
                <p className="text-xs text-[var(--leo-text-dim)]">{blockId}</p>
                <p className="text-lg font-semibold">{last ? `${(last.soc_after * 100).toFixed(0)}%` : "—"}</p>
                <Sparkline points={soc} color="var(--leo-accent)" min={0.1} max={0.95} />
              </div>
            );
          })}
        </div>
      </section>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3">Far-end voltage by phase</h2>
        <div className="grid grid-cols-3 gap-3">
          {sensorIds.map((id) => {
            const rows = sensors.filter((s) => s.sensor_id === id);
            const v = rows.map((r) => r.voltage_v);
            const last = rows[rows.length - 1];
            return (
              <div key={id} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-3">
                <p className="text-xs text-[var(--leo-text-dim)]">{id}</p>
                <p className="text-lg font-semibold">{last ? `${last.voltage_v.toFixed(1)}V` : "—"}</p>
                <Sparkline points={v} color="var(--leo-live)" min={210} max={250} />
              </div>
            );
          })}
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3">
          Live-rule triggers ({ruleTriggers.length})
        </h2>
        {ruleTriggers.length === 0 && (
          <p className="text-sm text-[var(--leo-text-dim)]">
            None today — the approved plan held without a live correction.
          </p>
        )}
        {ruleTriggers.slice(0, 10).map((r, i) => (
          <div key={i} className="flex justify-between text-xs border-b border-[var(--leo-border)] py-1.5">
            <span>{r.block_id}</span>
            <span>{new Date(r.ts_end).toLocaleTimeString("en-IN", { hour12: false })}</span>
            <span className="text-[var(--leo-warn)]">{r.rule_triggered}</span>
          </div>
        ))}
      </section>
    </main>
  );
}
