"use client";

/**
 * Operator console. Three columns tell one story:
 *   left   — what the day-ahead forecast PREDICTED, and why;
 *   centre — the feeder right now (actual, or what was forecast);
 *   right  — what LEO DID about it, and why, in time order.
 * The banner row says what's happening at the playhead in plain words.
 */

import { useEffect, useMemo, useState } from "react";
import FeederMap from "@/components/map/FeederMap";
import Timeline, { TimelineEventKind, TimelineEventMarker } from "@/components/timeline/Timeline";
import ForecastPanel from "@/components/operator/ForecastPanel";
import ActionLog from "@/components/operator/ActionLog";
import Glossary from "@/components/operator/Glossary";
import { ActionItem, EVENT_STYLE, Forecast, PredictedEvent, RunMeta, fmtTime, ms } from "@/components/operator/types";

const DT_ID = process.env.NEXT_PUBLIC_DT_ID ?? "DT-0417";
const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const PHASES = ["R", "Y", "B"] as const;
const PHASE_TEXT_COLOR: Record<string, string> = { R: "#e0473e", Y: "#e0a72e", B: "#3ba9ff" };

const RUN_GROUPS: { title: string; runs: string[] }[] = [
  { title: "Peak day · 27 Apr", runs: ["normal", "baseline"] },
  { title: "Outages · 27 Apr", runs: ["load_shedding", "outage"] },
  { title: "Sunny day · 11 Feb", runs: ["surplus", "surplus_baseline"] },
];
const SHORT_LABEL: Record<string, string> = {
  normal: "With LEO", baseline: "Without LEO", load_shedding: "Load shedding", outage: "Unplanned",
  surplus: "With LEO", surplus_baseline: "Without LEO",
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

type Banner = { tone: "bad" | "warn" | "info" | "ok"; title: string; body: string };

export default function OperatorHome() {
  const [catalog, setCatalog] = useState<RunMeta[]>([]);
  const [catalogError, setCatalogError] = useState(false);
  const [runId, setRunId] = useState("normal");
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
  const liveTrafo = useMemo(() => {
    if (!forecast || !currentTs) return null;
    const past = forecast.intervals.filter((i) => ms(i.ts_end) <= ms(currentTs) && i.trafo_actual_pct != null);
    return past.length ? past[past.length - 1].trafo_actual_pct : null;
  }, [forecast, currentTs]);

  const now = currentTs ? ms(currentTs) : 0;
  const activeItems = actions.filter((a) => a.end && ms(a.ts) <= now && now < ms(a.end));
  const currentMode = useMemo(() => {
    const modes = actions.filter((a) => a.kind === "mode" && ms(a.ts) <= now);
    return modes.length ? String(modes[modes.length - 1].meta.to) : "normal";
  }, [actions, now]);

  const banners: Banner[] = useMemo(() => {
    const out: Banner[] = [];
    const leoNow = activeItems.filter((a) => a.kind.startsWith("battery_") || a.kind === "dr_window")
      .map((a) => a.kind === "dr_window" ? "DR window live" : a.title.replace(/ \d\d:\d\d–\d\d:\d\d IST.*$/, ""));
    const leoText = run?.leo_enabled ? (leoNow.length ? `LEO is: ${leoNow.join(" · ")}.` : "LEO: no battery/DR action this interval.") : "LEO is off on this run.";
    if (currentMode === "backup" || currentMode === "restoration") {
      const b = actions.find((a) => a.kind === "backup_start");
      out.push({ tone: "bad", title: currentMode === "backup" ? "GRID DOWN — running on backup" : "GRID BACK — staggered re-transfer",
        body: `${b?.title ?? ""}. ${b?.detail ?? ""}` });
    } else if (currentMode === "pre_outage") {
      const notice = actions.find((a) => a.kind === "load_shedding_notice");
      out.push({ tone: "warn", title: "PRE-OUTAGE — load shedding scheduled",
        body: `${notice?.title ?? ""}. Batteries hold and charge instead of discharging; all households were alerted by SMS.` });
    }
    for (const a of activeItems.filter((x) => x.actor === "Network")) {
      const ev = String(a.meta.type);
      out.push({ tone: "bad", title: `${(EVENT_STYLE[ev]?.label ?? ev).toUpperCase()}${a.meta.phase ? ` — phase ${a.meta.phase}` : ""}`,
        body: `${a.title}. ${ev === "transformer_overload" ? "" : `${violatingCount} bus points out of limits right now. `}${leoText}` });
    }
    if (!out.length) {
      const next = events.find((e) => ms(e.start) > now);
      if (next) {
        const mins = Math.round((ms(next.start) - now) / 60000);
        out.push({ tone: "info", title: `Next predicted: ${next.title}`,
          body: `Expected from ${fmtTime(next.start)} IST (in ${Math.floor(mins / 60)}h ${mins % 60}m). ${leoText}` });
      } else {
        out.push({ tone: "ok", title: "All within limits", body: leoText });
      }
    }
    return out;
  }, [activeItems, currentMode, actions, events, now, violatingCount, run?.leo_enabled]);

  const markers: TimelineEventMarker[] = actions
    .filter((a) => ["grid_loss", "grid_return", "load_shedding_notice", "dr_window", "recommendation"].includes(a.kind))
    .map((a) => ({
      ts: a.ts, label: a.title,
      kind: (a.kind === "grid_loss" ? "outage" : a.kind === "grid_return" ? "restoration" : a.kind === "dr_window" ? "dr_event" : "violation") as TimelineEventKind,
    }));
  const bands = events.map((e) => ({ startTs: e.start, endTs: e.end, color: EVENT_STYLE[e.type]?.color ?? "#93a1b0", label: `Predicted: ${e.title}` }));
  const doSeek = (ts: string) => setSeek({ ts, nonce: Date.now() });
  const floor = limits.nominal * (1 - limits.pct / 100), ceil = limits.nominal * (1 + limits.pct / 100);

  const TONE: Record<Banner["tone"], string> = {
    bad: "border-[var(--leo-bad)] bg-[var(--leo-bad)]/15", warn: "border-[var(--leo-warn)] bg-[var(--leo-warn)]/15",
    info: "border-[var(--leo-accent)] bg-[var(--leo-accent)]/10", ok: "border-[var(--leo-ok)] bg-[var(--leo-ok)]/10",
  };
  const TONE_TEXT: Record<Banner["tone"], string> = { bad: "text-[var(--leo-bad)]", warn: "text-[var(--leo-warn)]", info: "text-[var(--leo-accent)]", ok: "text-[var(--leo-ok)]" };

  return (
    <main id="main-content" className="px-4 py-3 flex flex-col gap-3 h-[calc(100vh-57px)] min-h-[720px]">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-base font-semibold mr-2">Operator console</h1>
        {RUN_GROUPS.map((g) => {
          const avail = g.runs.filter((r) => catalog.some((c) => c.run_id === r));
          if (!avail.length) return null;
          return (
            <div key={g.title} className="flex items-center gap-1.5">
              <span className="text-[11px] text-[var(--leo-text-dim)]">{g.title}</span>
              <div className="flex rounded-md border border-[var(--leo-border)] overflow-hidden">
                {avail.map((r) => (
                  <button key={r} onClick={() => setRunId(r)}
                    className={`px-2.5 py-1 text-xs ${runId === r ? "bg-[var(--leo-accent)] text-black" : "bg-[var(--leo-panel-raised)]"}`}>
                    {SHORT_LABEL[r]}
                  </button>
                ))}
              </div>
            </div>
          );
        })}
        <button onClick={() => setShowGlossary(true)}
          className="ml-auto text-xs rounded-md border border-[var(--leo-border)] px-2.5 py-1 hover:border-[var(--leo-accent)]">
          What am I looking at?
        </button>
      </div>

      {catalogError && (
        <div role="alert" className="text-sm text-[var(--leo-bad)]">Couldn&apos;t reach the cloud API — is the stack running?</div>
      )}

      <div className="flex flex-col gap-1.5" role="status" aria-live="polite">
        {banners.map((b, i) => (
          <div key={i} className={`rounded-md border px-3 py-2 text-sm flex gap-3 items-baseline ${TONE[b.tone]}`}>
            <span className={`font-semibold whitespace-nowrap ${TONE_TEXT[b.tone]}`}>{b.title}</span>
            <span className="text-[var(--leo-text)]">{b.body}</span>
          </div>
        ))}
      </div>

      {run && (
        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)_360px] gap-3">
          <ForecastPanel forecast={forecast} events={events} actions={actions} startTs={run.sim_start} endTs={run.sim_end}
            currentTs={currentTs} leoEnabled={run.leo_enabled} onSeek={doSeek} />

          <div className="flex flex-col gap-2 min-h-[360px]">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-[var(--leo-text-dim)]">Map shows</span>
              <div className="flex rounded-md border border-[var(--leo-border)] overflow-hidden">
                {[false, true].map((f) => (
                  <button key={String(f)} onClick={() => setMapForecast(f)}
                    className={`px-2 py-0.5 ${mapForecast === f ? "bg-[var(--leo-accent)] text-black" : "bg-[var(--leo-panel-raised)]"}`}>
                    {f ? "Forecast (predicted the day before)" : "What actually happened"}
                  </button>
                ))}
              </div>
              <span className="ml-auto text-[var(--leo-text-dim)]">Mode: <span className="text-[var(--leo-text)] font-medium">{currentMode.replace("_", "-")}</span></span>
            </div>
            <div className="flex-1 min-h-0">
              <FeederMap dtId={DT_ID} runId={run.run_id} ts={currentTs ?? undefined} backupBusIds={backupBusIds}
                liveDispatchByPhase={liveDispatch} onViolatingCount={setViolatingCount} forecast={mapForecast} />
            </div>
            <div className="grid grid-cols-2 xl:grid-cols-4 gap-2 text-xs">
              {PHASES.map((p) => {
                const v = liveVoltage[p];
                const d = liveDispatch[p];
                const bad = v?.voltage_v != null && (v.voltage_v < floor || v.voltage_v > ceil);
                const act = !d ? "—" : d.mode === "backup" ? "backup port" : d.actual_kw > 0.05 ? `discharging ${d.actual_kw.toFixed(1)} kW`
                  : d.actual_kw < -0.05 ? `charging ${(-d.actual_kw).toFixed(1)} kW` : "idle";
                return (
                  <div key={p} className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-2.5 py-1.5">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold" style={{ color: PHASE_TEXT_COLOR[p] }}>Phase {p}</span>
                      <span className={bad ? "text-[var(--leo-bad)] font-medium" : ""}>
                        {v ? (v.supply_present && v.voltage_v != null ? `${v.voltage_v.toFixed(0)} V far end` : "no supply") : "—"}
                      </span>
                    </div>
                    <div className="text-[var(--leo-text-dim)]">
                      Battery {act}{d && d.mode !== "backup" ? ` · ${(d.soc_after * 100).toFixed(0)}%` : ""}
                      {d?.rule_triggered && <span className="text-[var(--leo-warn)]"> · {d.rule_triggered.replace(/_/g, " ")}</span>}
                    </div>
                  </div>
                );
              })}
              <div className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-2.5 py-1.5">
                <div className="font-semibold">Transformer</div>
                <div className={liveTrafo != null && liveTrafo > 100 ? "text-[var(--leo-bad)] font-medium" : "text-[var(--leo-text-dim)]"}>
                  {liveTrafo != null ? `${liveTrafo.toFixed(0)}% of rating` : currentMode === "backup" ? "de-energised" : "—"}
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
