"use client";

/**
 * Operator console. Three columns tell one story:
 *   left   — what the day-ahead forecast PREDICTED, and why;
 *   centre — the feeder right now (actual, or what was forecast);
 *   right  — what LEO DID about it, and why, in time order.
 * The banner row says what's happening at the playhead in plain words.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import FeederMap from "@/components/map/FeederMap";
import Timeline, { TimelineEventKind, TimelineEventMarker } from "@/components/timeline/Timeline";
import ForecastPanel from "@/components/operator/ForecastPanel";
import ActionLog from "@/components/operator/ActionLog";
import Glossary from "@/components/operator/Glossary";
import PhaseChip from "@/components/operator/PhaseChip";
import { ActionItem, EVENT_STYLE, Forecast, PredictedEvent, RunMeta, fmtTime, ms } from "@/components/operator/types";

const DT_ID = process.env.NEXT_PUBLIC_DT_ID ?? "DT-0417";
const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const PHASES = ["R", "Y", "B"] as const;
/** Recorded scenarios. Two have a "without LEO" recording of the same day for comparison. */
const SCENARIOS: { id: string; label: string; withLeo: string; withoutLeo?: string }[] = [
  { id: "peak", label: "Peak day, 27 Apr", withLeo: "normal", withoutLeo: "baseline" },
  { id: "shed", label: "Announced load shedding, 27 Apr", withLeo: "load_shedding" },
  { id: "outage", label: "Unplanned outage, 27 Apr", withLeo: "outage" },
  { id: "sunny", label: "Sunny surplus day, 11 Feb", withLeo: "surplus", withoutLeo: "surplus_baseline" },
];
const MODE_LABEL: Record<string, string> = {
  normal: "Normal", pre_outage: "Pre-outage", backup: "Backup", restoration: "Restoration",
};
const RULE_TEXT: Record<string, string> = {
  peak_shave: "live peak shaving", absorb_surplus: "soaking up solar surplus",
  valley_fill: "filling the overnight dip", overvoltage: "holding voltage down",
};

type DispatchRow = { block_id: string; ts_end: string; setpoint_kw: number; actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null };
type SensorRow = { sensor_id: string; ts_end: string; voltage_v: number | null; supply_present: boolean };

function latestByKey<T extends { ts_end: string }>(rows: T[], keyOf: (r: T) => string, ts: string | null): Record<string, T> {
  if (!ts) return {};
  const t = ms(ts);
  const out: Record<string, T> = {};
  for (const r of rows) {
    if (ms(r.ts_end) > t) continue;
    const k = keyOf(r);
    if (!out[k] || ms(r.ts_end) > ms(out[k].ts_end)) out[k] = r;
  }
  return out;
}

const getJSON = <T,>(path: string, fallback: T): Promise<T> =>
  fetch(`${CLOUD_API_URL}${path}`).then((r) => (r.ok ? r.json() : fallback)).catch(() => fallback);

type Banner = { tone: "bad" | "warn" | "info" | "ok"; title: string; body: string[] };

export default function OperatorHome() {
  const [catalog, setCatalog] = useState<RunMeta[]>([]);
  const [catalogError, setCatalogError] = useState(false);
  const [scenarioId, setScenarioId] = useState("peak");
  const [withLeo, setWithLeo] = useState(true);
  const scenario = SCENARIOS.find((s) => s.id === scenarioId)!;
  const runId = withLeo || !scenario.withoutLeo ? scenario.withLeo : scenario.withoutLeo;
  const compareRunId = scenario.withoutLeo && runId === scenario.withLeo ? scenario.withoutLeo : null;
  const [compareForecast, setCompareForecast] = useState<Forecast | null>(null);
  const [showMore, setShowMore] = useState(false);
  const [planStatus, setPlanStatus] = useState<string | null>(null);
  useEffect(() => {
    setPlanStatus(null);
    getJSON<{ status?: string } | null>(`/api/plan/${scenario.withLeo}`, null).then((p) => setPlanStatus(p?.status ?? null));
  }, [scenario.withLeo]);
  const [currentTs, setCurrentTs] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [seek, setSeek] = useState<{ ts: string; nonce: number } | null>(null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [predicted, setPredicted] = useState<PredictedEvent[]>([]);
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [dispatch, setDispatch] = useState<DispatchRow[]>([]);
  const [sensors, setSensors] = useState<SensorRow[]>([]);
  const [backupBusIds, setBackupBusIds] = useState<Set<string>>(new Set());
  const [violatingCount, setViolatingCount] = useState(0);
  const [mapForecast, setMapForecast] = useState(false);
  const [showGlossary, setShowGlossary] = useState(false);
  const [limits, setLimits] = useState({ nominal: 250, pct: 6 });

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/run_catalog`)
      .then((r) => { if (!r.ok) throw new Error(); return r.json(); })
      .then(setCatalog)
      .catch(() => setCatalogError(true));
    getJSON<{ properties?: { nominal_v_ln: number; v_limit_pct: number } }>(`/api/feeder/${DT_ID}`, {})
      .then((d) => d.properties && setLimits({ nominal: d.properties.nominal_v_ln, pct: d.properties.v_limit_pct }));
  }, []);

  const run = catalog.find((r) => r.run_id === runId);

  useEffect(() => {
    if (!run) return;
    setCurrentTs(run.sim_start);
    setForecast(null); setPredicted([]); setActions([]); setDispatch([]); setSensors([]);
    getJSON<Forecast | null>(`/api/forecast/${run.run_id}`, null).then(setForecast);
    getJSON<PredictedEvent[]>(`/api/predicted_events/${run.run_id}`, []).then(setPredicted);
    getJSON<ActionItem[]>(`/api/action_log/${run.run_id}`, []).then(setActions);
    getJSON<DispatchRow[]>(`/api/dispatch/${run.run_id}`, []).then(setDispatch);
    getJSON<SensorRow[]>(`/api/sensor_readings/${run.run_id}`, []).then(setSensors);
    getJSON<{ bus_id: string }[]>(`/api/backup_households/${run.run_id}`, [])
      .then((rows) => setBackupBusIds(new Set(rows.map((r) => r.bus_id))));
  }, [run?.run_id]);

  // The same day recorded without LEO, for a "without LEO at this moment" comparison.
  useEffect(() => {
    setCompareForecast(null);
    if (compareRunId) getJSON<Forecast | null>(`/api/forecast/${compareRunId}`, null).then(setCompareForecast);
  }, [compareRunId]);

  // An unplanned outage can't be on the forecast — add it as a card once it exists in the log.
  const events: PredictedEvent[] = useMemo(() => {
    const loss = actions.find((a) => a.kind === "grid_loss" && a.meta?.planned === false);
    const back = actions.find((a) => a.kind === "grid_return");
    if (!loss) return predicted;
    const outageCard: PredictedEvent = {
      type: "unplanned_outage", phase: null, severity: "high", title: "Unplanned outage — upstream fault",
      predicted_at: null, start: loss.ts, end: back?.ts ?? loss.ts, worst_ts: loss.ts,
      why: ["Not forecastable: faults give no warning", "Detected the same interval by all three busbar sensors losing supply"],
      actual: { start: loss.ts, end: back?.ts ?? loss.ts, worst: null }, worst: null,
    };
    return [...predicted, outageCard].sort((a, b) => ms(a.start) - ms(b.start));
  }, [predicted, actions]);

  const liveDispatch = useMemo(() => latestByKey(dispatch, (r) => r.block_id.replace("BATT-", ""), currentTs), [dispatch, currentTs]);
  const liveVoltage = useMemo(
    () => latestByKey(sensors.filter((s) => s.sensor_id.includes("FAREND")), (r) => r.sensor_id.replace("SEN-FAREND-", ""), currentTs),
    [sensors, currentTs]
  );
  // Transformer loading at the playhead only: never carried forward across a gap (an outage).
  const trafoAt = (f: Forecast | null) => {
    if (!f || !currentTs) return null;
    const t = ms(currentTs);
    let best: number | null = null;
    for (const i of f.intervals) if (ms(i.ts_end) <= t && t - ms(i.ts_end) < 15 * 60_000) best = i.trafo_actual_pct;
    return best;
  };
  const liveTrafo = useMemo(() => trafoAt(forecast), [forecast, currentTs]); // eslint-disable-line react-hooks/exhaustive-deps
  const withoutLeoTrafo = useMemo(() => trafoAt(compareForecast), [compareForecast, currentTs]); // eslint-disable-line react-hooks/exhaustive-deps

  const now = currentTs ? ms(currentTs) : 0;
  const activeItems = actions.filter((a) => a.end && ms(a.ts) <= now && now < ms(a.end));
  const currentMode = useMemo(() => {
    const modes = actions.filter((a) => a.kind === "mode" && ms(a.ts) <= now);
    return modes.length ? String(modes[modes.length - 1].meta.to) : "normal";
  }, [actions, now]);

  // One situation line, most severe first. Every number is the value at the playhead, never a future peak.
  const banners: Banner[] = useMemo(() => {
    const out: Banner[] = [];
    const floorV = limits.nominal * (1 - limits.pct / 100), ceilV = limits.nominal * (1 + limits.pct / 100);

    const leoLine = (() => {
      if (!run?.leo_enabled) return "LEO is switched off in this recording, for comparison.";
      const parts: string[] = [];
      const dis = PHASES.filter((p) => (liveDispatch[p]?.actual_kw ?? 0) > 0.05 && liveDispatch[p]?.mode !== "backup");
      const chg = PHASES.filter((p) => (liveDispatch[p]?.actual_kw ?? 0) < -0.05);
      if (dis.length) parts.push(`batteries discharging on ${dis.join(", ")}`);
      if (chg.length) parts.push(`charging on ${chg.join(", ")}`);
      const drw = activeItems.find((x) => x.kind === "dr_window");
      if (drw) parts.push(`DR window open on phase ${drw.meta.phase}`);
      if (activeItems.some((x) => x.kind === "dr_auto_ac")) parts.push("enrolled ACs cycled down");
      return parts.length ? `LEO now: ${parts.join("; ")}.` : "LEO: no battery or DR action this interval.";
    })();

    if (currentMode === "backup") {
      const loss = [...actions].reverse().find((a) => a.kind === "grid_loss" && ms(a.ts) <= now);
      const socs = PHASES.map((p) => liveDispatch[p]).filter((d) => d?.mode === "backup").map((d) => d!.soc_after * 100);
      out.push({ tone: "bad", title: `Grid down. ${backupBusIds.size} critical premises on backup power`,
        body: [`${loss?.meta?.planned ? "Scheduled load shedding" : "Unplanned fault"} since ${loss ? fmtTime(loss.ts) : "—"} IST. The rest of the feeder has no supply.`,
          socs.length ? `Backup batteries ${socs.map((v) => `${v.toFixed(0)}%`).join(" / ")} charged.` : ""].filter(Boolean) });
    } else if (currentMode === "restoration") {
      out.push({ tone: "warn", title: "Grid back. Reconnecting backup premises in small batches",
        body: ["Staggered so the returning load does not trip the transformer."] });
    } else if (currentMode === "pre_outage") {
      const notice = actions.find((a) => a.kind === "load_shedding_notice");
      out.push({ tone: "warn", title: notice?.title.replace("DISCOM published a load-shedding schedule", "Load shedding announced") ?? "Load shedding announced",
        body: ["Batteries hold their charge for the cut instead of discharging. All households were told by SMS."] });
    }

    const net = activeItems.filter((x) => x.actor === "Network");
    if (net.length && currentMode !== "backup") {
      const parts: string[] = [];
      if (net.some((x) => x.meta.type === "transformer_overload") && liveTrafo != null && liveTrafo > 100)
        parts.push(`Transformer at ${liveTrafo.toFixed(0)}% of rating`);
      const lowPh = PHASES.filter((p) => { const v = liveVoltage[p]?.voltage_v; return v != null && v < floorV; });
      const highPh = PHASES.filter((p) => { const v = liveVoltage[p]?.voltage_v; return v != null && v > ceilV; });
      if (lowPh.length) parts.push(`low voltage on phase ${lowPh.join(", ")}`);
      if (highPh.length) parts.push(`high voltage on phase ${highPh.join(", ")}`);
      if (!parts.length) parts.push(`${violatingCount} points outside voltage limits`);
      const vs = [...lowPh, ...highPh].map((p) => `${p} ${liveVoltage[p]!.voltage_v!.toFixed(0)} V`);
      const body = [
        `${vs.length ? `Far-end voltage ${vs.join(", ")} (limits ${floorV.toFixed(0)}–${ceilV.toFixed(0)} V). ` : ""}${violatingCount} of the feeder's points are outside limits.`,
        leoLine,
      ];
      if (withoutLeoTrafo != null && liveTrafo != null) body.push(`Without LEO at this moment the transformer was at ${withoutLeoTrafo.toFixed(0)}% of rating.`);
      const t = parts.join(", ");
      out.push({ tone: "bad", title: t.charAt(0).toUpperCase() + t.slice(1), body });
    }

    if (!out.length) {
      const next = events.find((e) => ms(e.start) > now);
      const body = [leoLine];
      if (withoutLeoTrafo != null && liveTrafo != null && withoutLeoTrafo > 100)
        body.push(`Without LEO at this moment the transformer was at ${withoutLeoTrafo.toFixed(0)}% of rating.`);
      if (next) {
        const mins = Math.round((ms(next.start) - now) / 60000);
        out.push({ tone: "info", title: `Within limits. Next forecast problem in ${Math.floor(mins / 60)} h ${mins % 60} min`,
          body: [`${next.title}, expected from ${fmtTime(next.start)} IST.`, ...body] });
      } else {
        out.push({ tone: "ok", title: "Within limits", body });
      }
    }
    return out;
  }, [activeItems, currentMode, actions, events, now, violatingCount, run?.leo_enabled, liveDispatch, liveVoltage, liveTrafo, withoutLeoTrafo, backupBusIds, limits]);

  const markers: TimelineEventMarker[] = actions
    .filter((a) => ["grid_loss", "grid_return", "load_shedding_notice", "dr_window", "recommendation"].includes(a.kind))
    .map((a) => ({
      ts: a.ts, label: a.title,
      kind: (a.kind === "grid_loss" ? "outage" : a.kind === "grid_return" ? "restoration" : a.kind === "dr_window" ? "dr_event" : a.kind === "recommendation" ? "escalation" : "violation") as TimelineEventKind,
    }));
  const bands = events.filter((e) => e.predicted_at).map((e) => ({ startTs: e.start, endTs: e.end, color: EVENT_STYLE[e.type]?.color ?? "#93a1b0", label: `Predicted: ${e.title}` }));
  const doSeek = (ts: string) => setSeek({ ts, nonce: Date.now() });
  const floor = limits.nominal * (1 - limits.pct / 100), ceil = limits.nominal * (1 + limits.pct / 100);

  const TONE: Record<Banner["tone"], string> = {
    bad: "border-[var(--leo-bad-fill)] bg-[rgb(224_71_62/0.12)]", warn: "border-[var(--leo-warn)] bg-[rgb(224_167_46/0.10)]",
    info: "border-[var(--leo-border)] bg-[var(--leo-panel)]", ok: "border-[var(--leo-border)] bg-[var(--leo-panel)]",
  };
  const TONE_ICON: Record<Banner["tone"], string> = { bad: "!", warn: "!", info: "i", ok: "✓" };
  const TONE_TEXT: Record<Banner["tone"], string> = { bad: "text-[var(--leo-bad)]", warn: "text-[var(--leo-warn)]", info: "text-[var(--leo-text)]", ok: "text-[var(--leo-ok)]" };

  return (
    <main id="main-content" className="px-4 py-3 flex flex-col gap-3 h-[calc(100dvh-50px)] min-h-[680px]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="sr-only">Operator console: map and replay</h1>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-[var(--leo-text-dim)]">Recording</span>
          <select value={scenarioId} onChange={(e) => { setScenarioId(e.target.value); setShowMore(false); }}
            className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-2.5 py-1.5 text-sm">
            {SCENARIOS.filter((sc) => catalog.length === 0 || catalog.some((c) => c.run_id === sc.withLeo)).map((sc) => (
              <option key={sc.id} value={sc.id}>{sc.label}</option>
            ))}
          </select>
        </label>
        {scenario.withoutLeo && (
          <div role="group" aria-label="Compare" className="flex rounded-md border border-[var(--leo-border)] p-0.5">
            {[true, false].map((w) => (
              <button key={String(w)} type="button" aria-pressed={withLeo === w} onClick={() => setWithLeo(w)}
                className={`rounded px-3 py-1 text-sm ${withLeo === w ? "bg-[var(--leo-panel-raised)] text-[var(--leo-text)] shadow-[inset_0_0_0_1px_var(--leo-border)]" : "text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"}`}>
                {w ? "With LEO" : "Without LEO"}
              </button>
            ))}
          </div>
        )}
        <span className="text-sm text-[var(--leo-text-dim)]">
          LEO mode <span className="font-medium text-[var(--leo-text)]">{run?.leo_enabled ? MODE_LABEL[currentMode] ?? currentMode : "Off"}</span>
        </span>
        {planStatus && (
          <Link href={`/operator/plan${scenario.withLeo === "surplus" ? "?run=surplus" : ""}`}
            className={`rounded-md border px-3 py-1.5 text-sm hover:bg-[var(--leo-panel-raised)] ${planStatus === "pending" ? "border-[var(--leo-warn)] text-[var(--leo-warn)]" : planStatus === "rejected" ? "border-[var(--leo-bad-fill)] text-[var(--leo-bad)]" : "border-[var(--leo-border)]"}`}>
            Day plan: <span className="font-medium">{planStatus === "approved" ? "approved" : planStatus === "rejected" ? "rejected" : "needs your decision"}</span> ›
          </Link>
        )}
        <button type="button" onClick={() => setShowGlossary(true)}
          className="ml-auto rounded-md border border-[var(--leo-border)] px-3 py-1.5 text-sm hover:bg-[var(--leo-panel-raised)]">
          What am I looking at?
        </button>
      </div>

      {catalogError && (
        <div role="alert" className="text-sm"><span className="font-medium text-[var(--leo-bad)]">The recordings could not be loaded.</span> <span className="text-[var(--leo-text-dim)]">Check that the cloud API is running on {CLOUD_API_URL}, then reload.</span></div>
      )}

      {banners[0] && (
        <section aria-label="Situation now" className={`rounded-md border px-3 py-2 text-sm ${TONE[banners[0].tone]}`}>
          <p role="status" className="sr-only">{banners[0].title}</p>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span aria-hidden className={`inline-flex h-4 w-4 shrink-0 items-center justify-center self-center rounded-full border text-[11px] font-bold ${TONE_TEXT[banners[0].tone]} border-current`}>{TONE_ICON[banners[0].tone]}</span>
            <span className={`font-semibold ${TONE_TEXT[banners[0].tone]}`}>{banners[0].title}</span>
            <span className="text-[var(--leo-text)]">{banners[0].body.join(" ")}</span>
            {banners.length > 1 && (
              <button type="button" aria-expanded={showMore} onClick={() => setShowMore((v) => !v)}
                className="ml-auto text-[13px] text-[var(--leo-text-dim)] underline underline-offset-2 hover:text-[var(--leo-text)]">
                {showMore ? "Show less" : `${banners.length - 1} more`}
              </button>
            )}
          </div>
          {showMore && banners.slice(1).map((b, i) => (
            <div key={i} className="mt-1.5 flex flex-wrap items-baseline gap-x-3 border-t border-[var(--leo-border)] pt-1.5 pl-7">
              <span className={`font-semibold ${TONE_TEXT[b.tone]}`}>{b.title}</span>
              <span>{b.body.join(" ")}</span>
            </div>
          ))}
        </section>
      )}

      {run && (
        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)_360px] gap-3">
          <ForecastPanel forecast={forecast} events={events} actions={actions} startTs={run.sim_start} endTs={run.sim_end}
            currentTs={currentTs} leoEnabled={run.leo_enabled} onSeek={doSeek} />

          <div className="flex flex-col gap-2 min-h-[360px]">
            <div className="flex items-center gap-2 text-[13px]">
              <span className="text-[var(--leo-text-dim)]">Map shows</span>
              <div role="group" aria-label="Map shows" className="flex rounded-md border border-[var(--leo-border)] p-0.5">
                {[false, true].map((f) => (
                  <button key={String(f)} type="button" aria-pressed={mapForecast === f} onClick={() => setMapForecast(f)}
                    className={`rounded px-2.5 py-0.5 ${mapForecast === f ? "bg-[var(--leo-panel-raised)] text-[var(--leo-text)] shadow-[inset_0_0_0_1px_var(--leo-border)]" : "text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"}`}>
                    {f ? "Forecast from the day before" : "What happened"}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex-1 min-h-0">
              <FeederMap dtId={DT_ID} runId={run.run_id} ts={currentTs ?? undefined} backupBusIds={backupBusIds}
                liveDispatchByPhase={liveDispatch} onViolatingCount={setViolatingCount} forecast={mapForecast} />
            </div>
            <div className="grid grid-cols-2 gap-2 text-[13px] xl:grid-cols-4">
              {PHASES.map((p) => {
                const v = liveVoltage[p];
                const d = liveDispatch[p];
                const bad = v?.voltage_v != null && (v.voltage_v < floor || v.voltage_v > ceil);
                const act = !d ? "Battery: no data yet" : d.mode === "backup" ? "Battery feeding backup" : d.actual_kw > 0.05 ? `Discharging ${d.actual_kw.toFixed(1)} kW`
                  : d.actual_kw < -0.05 ? `Charging ${(-d.actual_kw).toFixed(1)} kW` : "Battery idle";
                return (
                  <div key={p} className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-2.5 py-1.5">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                      <PhaseChip phase={p} />
                      <span className={`whitespace-nowrap ${bad ? "font-semibold text-[var(--leo-bad)]" : ""}`}>
                        {v ? (v.supply_present && v.voltage_v != null ? `${v.voltage_v.toFixed(0)} V${bad ? (v.voltage_v < floor ? " low" : " high") : ""}` : "off") : "—"}
                      </span>
                    </div>
                    <div className="text-[var(--leo-text-dim)]">
                      {act}{d && d.mode !== "backup" ? ` · ${(d.soc_after * 100).toFixed(0)}%` : ""}
                      {d?.rule_triggered && <span className="block text-[var(--leo-text)]">{(RULE_TEXT[d.rule_triggered] ?? d.rule_triggered.replace(/_/g, " ")).replace(/^./, (c) => c.toUpperCase())}</span>}
                    </div>
                  </div>
                );
              })}
              <div className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-2.5 py-1.5">
                <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                  <span className="font-semibold">Transformer</span>
                  <span className={liveTrafo != null && liveTrafo > 100 ? "font-semibold text-[var(--leo-bad)]" : ""}>
                    {currentMode === "backup" ? "off" : liveTrafo != null ? `${liveTrafo.toFixed(0)}%` : "—"}
                  </span>
                </div>
                <div className="text-[var(--leo-text-dim)]">
                  {currentMode === "backup" ? "De-energised, grid down" : withoutLeoTrafo != null ? `of rating · ${withoutLeoTrafo.toFixed(0)}% without LEO` : "of rating"}
                </div>
              </div>
            </div>
          </div>

          <ActionLog items={actions} currentTs={currentTs} startTs={run.sim_start} playing={playing} onSeek={doSeek} />
        </div>
      )}

      {run && (
        <Timeline key={run.run_id} runId={run.run_id} startTs={run.sim_start} endTs={run.sim_end}
          segments={[{ kind: "recorded", startTs: run.sim_start, endTs: run.sim_end }]}
          events={markers} bands={bands} initialTs={run.sim_start} onScrub={setCurrentTs}
          seekTo={seek} onPlayingChange={setPlaying} />
      )}

      {showGlossary && <Glossary runId={runId} onClose={() => setShowGlossary(false)} />}
    </main>
  );
}
