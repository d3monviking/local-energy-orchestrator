"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import DayChart, { type Series } from "@/components/operator/DayChart";
import PhaseChip from "@/components/operator/PhaseChip";
import RunSelect, { LoadState, getJSON, useRunParam } from "@/components/operator/RunSelect";
import { fmtTime, ms, type RunMeta } from "@/components/operator/types";

const RUNS = ["normal", "load_shedding", "outage", "surplus"];
const PHASES = ["R", "Y", "B"] as const;
const LINE: Record<string, { color: string; dash?: string }> = {
  R: { color: "#c98f8b" }, Y: { color: "#c9b77f", dash: "7 4" }, B: { color: "#8ea9cf", dash: "2 3" },
};
const RULE_TEXT: Record<string, string> = {
  peak_shave: "Shaved a peak the plan did not expect",
  absorb_surplus: "Soaked up surplus solar",
  valley_fill: "Charged in the overnight dip",
  overvoltage: "Held voltage down",
};

type DispatchRow = { block_id: string; ts_end: string; setpoint_kw: number; actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null };
type SensorRow = { sensor_id: string; ts_end: string; voltage_v: number | null; supply_present: boolean };

export default function LiveOperations() {
  const [run, setRun] = useRunParam(RUNS);
  const [meta, setMeta] = useState<RunMeta | null>(null);
  const [dispatch, setDispatch] = useState<DispatchRow[] | null>(null);
  const [sensors, setSensors] = useState<SensorRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setDispatch(null);
    try {
      const [cat, d, s] = await Promise.all([
        getJSON<RunMeta[]>("/api/run_catalog"), getJSON<DispatchRow[]>(`/api/dispatch/${run}`), getJSON<SensorRow[]>(`/api/sensor_readings/${run}`),
      ]);
      setMeta(cat.find((c) => c.run_id === run) ?? null);
      setDispatch(d);
      setSensors(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run]);
  useEffect(() => { load(); }, [load]);

  const t0 = meta ? ms(meta.sim_start) : 0, t1 = meta ? ms(meta.sim_end) : 1;
  const phaseOf = (id: string) => id.slice(-1);

  const soc: Series[] = PHASES.map((p) => ({
    key: p, label: `Phase ${p}`, ...LINE[p],
    points: (dispatch ?? []).filter((d) => phaseOf(d.block_id) === p).map((d) => ({ t: ms(d.ts_end), v: d.soc_after * 100 })),
  }));
  const volts: Series[] = PHASES.map((p) => ({
    key: p, label: `Phase ${p}`, ...LINE[p],
    points: sensors.filter((s) => s.sensor_id === `SEN-FAREND-${p}`).map((s) => ({ t: ms(s.ts_end), v: s.supply_present ? s.voltage_v : null })),
  }));
  const vVals = volts.flatMap((s) => s.points.map((p) => p.v).filter((v): v is number => v != null));
  const vMin = Math.min(225, Math.floor((Math.min(...vVals, 235) - 3) / 5) * 5);
  const vMax = Math.max(270, Math.ceil((Math.max(...vVals, 265) + 3) / 5) * 5);

  // Consecutive intervals of the same live correction on a phase read as one episode.
  const episodes = useMemo(() => {
    const out: { phase: string; rule: string; start: string; end: string; n: number; maxShift: number }[] = [];
    const rows = [...(dispatch ?? [])].filter((d) => d.rule_triggered).sort((a, b) => a.block_id.localeCompare(b.block_id) || ms(a.ts_end) - ms(b.ts_end));
    for (const r of rows) {
      const p = phaseOf(r.block_id);
      const last = out[out.length - 1];
      const shift = Math.abs(r.actual_kw - r.setpoint_kw);
      if (last && last.phase === p && last.rule === r.rule_triggered && ms(r.ts_end) - ms(last.end) <= 15 * 60_000) {
        last.end = r.ts_end; last.n += 1; last.maxShift = Math.max(last.maxShift, shift);
      } else {
        out.push({ phase: p, rule: r.rule_triggered!, start: r.ts_end, end: r.ts_end, n: 1, maxShift: shift });
      }
    }
    return out.sort((a, b) => ms(a.start) - ms(b.start));
  }, [dispatch]);

  return (
    <main id="main-content" className="mx-auto w-full max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold leading-tight">Live</h1>
          <p className="mt-1 max-w-[70ch] text-sm text-[var(--leo-text-dim)]">
            Battery charge and far-end voltage on each phase through the day, as LEO&apos;s own sensors recorded them, and every time LEO
            changed the plan live.
          </p>
        </div>
        <RunSelect runs={RUNS} value={run} onChange={setRun} />
      </div>

      <LoadState error={error} loading={!error && !dispatch} onRetry={load} />

      {dispatch && meta && (
        <>
          <section aria-labelledby="soc" className="mt-8">
            <h2 id="soc" className="text-lg font-semibold">Battery charge</h2>
            <p className="mb-2 text-sm text-[var(--leo-text-dim)]">Percent of each phase battery&apos;s capacity, kept between 15% and 90% to protect the cells.</p>
            <DayChart series={soc} t0={t0} t1={t1} yMin={0} yMax={100} yStep={25} unit="%" label="Battery charge by phase through the day" />
          </section>

          <section aria-labelledby="volts" className="mt-10">
            <h2 id="volts" className="text-lg font-semibold">Voltage at the far end of each phase</h2>
            <p className="mb-2 text-sm text-[var(--leo-text-dim)]">The last home on the line sees the lowest voltage in the evening and the highest at sunny middays. Gaps mean no supply.</p>
            <DayChart series={volts} t0={t0} t1={t1} yMin={vMin} yMax={vMax} yStep={10} unit="V" height={240}
              refs={[{ v: 265, label: "upper limit 265 V" }, { v: 235, label: "lower limit 235 V" }]} label="Far-end voltage by phase through the day" />
          </section>

          <section aria-labelledby="live-fixes" className="mt-10">
            <h2 id="live-fixes" className="text-lg font-semibold">Live changes to the plan</h2>
            {episodes.length === 0 ? (
              <p className="mt-2 text-sm text-[var(--leo-text-dim)]">None. The approved plan held all day without a live correction.</p>
            ) : (
              <>
                <p className="mt-1 text-sm text-[var(--leo-text-dim)]">
                  {episodes.length} times a sensor reading made LEO depart from the approved plan, always within the network&apos;s safe limit.
                </p>
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full min-w-[640px] text-sm">
                    <thead className="text-left text-[13px] text-[var(--leo-text-dim)]">
                      <tr className="border-b border-[var(--leo-border)]">
                        <th className="py-2 pr-4 font-normal">When (IST)</th>
                        <th className="py-2 pr-4 font-normal">Phase</th>
                        <th className="py-2 pr-4 font-normal">What LEO did</th>
                        <th className="py-2 text-right font-normal">Largest change from plan</th>
                      </tr>
                    </thead>
                    <tbody>
                      {episodes.map((e, i) => (
                        <tr key={i} className="border-b border-[var(--leo-border)]">
                          <td className="py-2 pr-4">{fmtTime(new Date(ms(e.start) - 15 * 60_000).toISOString())}–{fmtTime(e.end)}</td>
                          <td className="py-2 pr-4"><PhaseChip phase={e.phase} /></td>
                          <td className="py-2 pr-4">{RULE_TEXT[e.rule] ?? e.rule.replace(/_/g, " ")}</td>
                          <td className="py-2 text-right">{e.maxShift.toFixed(1)} kW</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </section>
        </>
      )}
    </main>
  );
}
