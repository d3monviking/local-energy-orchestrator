"use client";

import { useEffect, useMemo, useState } from "react";
import FeederMap from "@/components/map/FeederMap";
import Timeline, { TimelineEventKind, TimelineEventMarker } from "@/components/timeline/Timeline";
import RecommendationCard, { Recommendation } from "@/components/recommendations/RecommendationCard";

const DT_ID = process.env.NEXT_PUBLIC_DT_ID ?? "DT-0417";
const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type RunInfo = { run_id: string; leo_enabled: boolean; sim_start: string; sim_end: string };
type DispatchRow = { block_id: string; ts_end: string; setpoint_kw: number; actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null };
type SensorRow = { sensor_id: string; ts_end: string; voltage_v: number; supply_present: boolean };
type RawEvent = { ts: string; kind: string };
type ViolationEpisode = {
  phase: string; start_ts: string; end_ts: string; n_intervals: number;
  kind: "overvoltage" | "undervoltage"; worst_voltage_v: number; deviation_v: number;
};
type DrEvent = {
  event_id: string; phase: string; window_start: string; window_end: string;
  target_kw: number; v_paise_kwh: number; n_sent: number; n_accepted: number; n_holdout: number;
};

const EVENT_KIND_MAP: Record<string, TimelineEventKind> = {
  grid_loss: "outage", power_fail: "outage", backup_start: "outage",
  grid_return: "restoration", restore: "restoration", backup_end: "restoration",
  overload_trip: "violation",
  dr_event_start: "dr_event", dr_event_end: "dr_event",
};

const RUN_LABEL: Record<string, string> = { normal: "With LEO", baseline: "Without LEO", outage: "Outage replay" };
const RUN_EXPLAINER: Record<string, string> = {
  normal: "Peak-demand day on an undersized 100 kVA transformer (150 homes, 250V nominal, ±6% limit). Voltage sags below the floor under heavy load. LEO discharges the three batteries per the approved plan and sends SMS DR offers on the worst phase (R). Phase R's undervoltage shrinks from 5.5h to 3.5h (17:00–20:30 IST). Phases Y and B barely move: each battery only has 5.25 kWh usable and empties it, can't recharge mid-day because those phases are already under the floor from 10:00 IST, and gets no DR — a ~11h, 25–33V gap is beyond local fixing, so it's escalated to the DISCOM as a tap-raise recommendation.",
  baseline: "Identical day, identical households, battery and DR disabled. Phase R undervoltage runs 16:00–21:30 IST — two hours longer than with LEO. Y and B look almost the same as With LEO, because LEO's local resources were too small to change them either.",
  outage: "Same day, an unplanned 90-minute upstream outage at 14:10 UTC (19:40 IST). Sensors go dark (no readings, not zero voltage), inverters anti-island (disconnect from the dead grid for safety), the backup circuit energises the one registered critical premise, and restoration staggers back on in batches rather than all at once.",
};
const PHASES = ["R", "Y", "B"] as const;
const PHASE_TEXT_COLOR: Record<string, string> = { R: "#e0473e", Y: "#e0a72e", B: "#3ba9ff" };

/** Last row at-or-before `ts`, per group key — a cheap client-side lookup
 * against a day's worth of already-fetched rows, so scrubbing/playing
 * never triggers a new network request. */
function latestByKey<T extends { ts_end: string }>(
  rows: T[], keyOf: (r: T) => string, ts: string | null
): Record<string, T> {
  if (!ts) return {};
  const tsMs = new Date(ts).getTime();
  const out: Record<string, T> = {};
  for (const r of rows) {
    if (new Date(r.ts_end).getTime() > tsMs) continue;
    const k = keyOf(r);
    if (!out[k] || new Date(r.ts_end).getTime() > new Date(out[k].ts_end).getTime()) out[k] = r;
  }
  return out;
}

function fmtTime(ts: string) {
  return new Date(ts).toLocaleTimeString("en-IN", { hour12: false });
}

export default function OperatorHome() {
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("normal");
  const [currentTs, setCurrentTs] = useState<string | null>(null);
  const [events, setEvents] = useState<TimelineEventMarker[]>([]);
  const [rawEvents, setRawEvents] = useState<RawEvent[]>([]);
  const [backupBusIds, setBackupBusIds] = useState<Set<string>>(new Set());
  const [runsError, setRunsError] = useState(false);
  const [dispatch, setDispatch] = useState<DispatchRow[]>([]);
  const [sensors, setSensors] = useState<SensorRow[]>([]);
  const [episodes, setEpisodes] = useState<ViolationEpisode[]>([]);
  const [drEvents, setDrEvents] = useState<DrEvent[]>([]);
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [violatingCount, setViolatingCount] = useState(0);
  // The DB's actual nominal is 250V, not the 230 a hardcoded fallback
  // would assume (confirmed in violation_episodes' server fix) - read
  // from the same feeder fetch FeederMap already makes, not guessed.
  const [nominalV, setNominalV] = useState(230);

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/feeder/${DT_ID}`)
      .then((r) => r.json())
      .then((d: { properties: { nominal_v_ln: number } }) => setNominalV(d.properties.nominal_v_ln))
      .catch(() => {});
  }, []);

  const loadRuns = () => {
    setRunsError(false);
    fetch(`${CLOUD_API_URL}/api/runs`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setRuns)
      .catch(() => {
        setRuns([]);
        setRunsError(true);
      });
  };

  useEffect(() => {
    loadRuns();
  }, []);

  const activeRun = runs.find((r) => r.run_id === selectedRunId);

  const loadRecs = (runId: string) => {
    fetch(`${CLOUD_API_URL}/api/recommendations/${runId}`).then((r) => r.json()).then(setRecs).catch(() => setRecs([]));
  };

  useEffect(() => {
    if (!activeRun) return;
    setCurrentTs(activeRun.sim_start);

    fetch(`${CLOUD_API_URL}/api/events/${activeRun.run_id}`)
      .then((r) => r.json())
      .then((rows: RawEvent[]) => {
        setRawEvents(rows);
        setEvents(
          rows
            .filter((r) => r.kind in EVENT_KIND_MAP)
            .map((r) => ({ ts: r.ts, kind: EVENT_KIND_MAP[r.kind], label: r.kind }))
        );
      })
      .catch(() => {
        setRawEvents([]);
        setEvents([]);
      });

    fetch(`${CLOUD_API_URL}/api/backup_households/${activeRun.run_id}`)
      .then((r) => r.json())
      .then((rows: { household_id: string; bus_id: string }[]) => setBackupBusIds(new Set(rows.map((r) => r.bus_id))))
      .catch(() => setBackupBusIds(new Set()));

    // Whole day, fetched once per run rather than re-fetched on every
    // scrub/play tick — latestByKey() does the "what's true right now"
    // lookup entirely client-side against this.
    fetch(`${CLOUD_API_URL}/api/dispatch/${activeRun.run_id}`).then((r) => r.json()).then(setDispatch).catch(() => setDispatch([]));
    fetch(`${CLOUD_API_URL}/api/sensor_readings/${activeRun.run_id}`).then((r) => r.json()).then(setSensors).catch(() => setSensors([]));
    fetch(`${CLOUD_API_URL}/api/violation_episodes/${activeRun.run_id}`).then((r) => r.json()).then(setEpisodes).catch(() => setEpisodes([]));
    fetch(`${CLOUD_API_URL}/api/dr_events/${activeRun.run_id}`).then((r) => r.json()).then(setDrEvents).catch(() => setDrEvents([]));
    loadRecs(activeRun.run_id);
  }, [activeRun?.run_id]);

  const liveDispatch = useMemo(
    () => latestByKey(dispatch, (r) => r.block_id.replace("BATT-", ""), currentTs),
    [dispatch, currentTs]
  );
  const liveVoltage = useMemo(
    () => latestByKey(sensors.filter((s) => s.sensor_id.includes("FAREND")), (r) => r.sensor_id.replace("SEN-FAREND-", ""), currentTs),
    [sensors, currentTs]
  );
  const lastEvent = useMemo(() => {
    if (!currentTs) return null;
    const tsMs = new Date(currentTs).getTime();
    const past = rawEvents.filter((e) => new Date(e.ts).getTime() <= tsMs);
    return past.length ? past[past.length - 1] : null;
  }, [rawEvents, currentTs]);

  const activeEpisodes = useMemo(() => {
    if (!currentTs) return [];
    const tsMs = new Date(currentTs).getTime();
    return episodes.filter((e) => tsMs >= new Date(e.start_ts).getTime() && tsMs < new Date(e.end_ts).getTime());
  }, [episodes, currentTs]);

  const activeDrEvent = useMemo(() => {
    if (!currentTs) return null;
    const tsMs = new Date(currentTs).getTime();
    return drEvents.find((e) => tsMs >= new Date(e.window_start).getTime() && tsMs < new Date(e.window_end).getTime()) ?? null;
  }, [drEvents, currentTs]);

  const activeOutage = useMemo(() => {
    if (selectedRunId !== "outage" || !currentTs) return null;
    const tsMs = new Date(currentTs).getTime();
    const loss = rawEvents.find((e) => e.kind === "grid_loss");
    const restore = rawEvents.find((e) => e.kind === "grid_return");
    if (!loss) return null;
    const lossMs = new Date(loss.ts).getTime();
    const restoreMs = restore ? new Date(restore.ts).getTime() : Infinity;
    return tsMs >= lossMs && tsMs < restoreMs ? { since: loss.ts } : null;
  }, [rawEvents, currentTs, selectedRunId]);

  const openRecs = recs.filter((r) => r.status !== "resolved");

  return (
    <main id="main-content" className="p-6 flex flex-col gap-4 h-[calc(100vh-57px)]">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Map + timeline</h1>
          <p className="text-sm text-[var(--leo-text-dim)] max-w-2xl">{RUN_EXPLAINER[selectedRunId]}</p>
        </div>

        <div className="flex rounded-md border border-[var(--leo-border)] overflow-hidden shrink-0">
          {["normal", "baseline", "outage"].map((runId) => (
            <button
              key={runId}
              onClick={() => setSelectedRunId(runId)}
              className={`px-3 py-1.5 text-sm ${selectedRunId === runId ? "bg-[var(--leo-accent)] text-black" : "bg-[var(--leo-panel-raised)]"}`}
            >
              {RUN_LABEL[runId]}
            </button>
          ))}
        </div>
      </div>

      {/* Live banner: outage > voltage violation > DR event, in that
          priority order, so the most operationally urgent thing at the
          current scrub position is what's shown — not everything
          stacked at once. */}
      {activeOutage && (
        <div role="alert" className="rounded-md border border-[var(--leo-bad)] bg-[var(--leo-bad)]/15 px-4 py-3 text-sm flex items-center gap-3">
          <span className="font-semibold text-[var(--leo-bad)]">⚠ UPSTREAM OUTAGE</span>
          <span>
            Since {fmtTime(activeOutage.since)} IST — sensors dark, inverters anti-islanded. Backup circuit energised for
            the registered critical premise.
          </span>
          <span className="ml-auto text-[var(--leo-text-dim)]">see Actions →</span>
        </div>
      )}
      {!activeOutage && activeEpisodes.length > 0 && (
        <div role="alert" className="rounded-md border border-[var(--leo-bad)] bg-[var(--leo-bad)]/15 px-4 py-3 text-sm flex items-center gap-3 flex-wrap">
          <span className="font-semibold text-[var(--leo-bad)]">⚠ VOLTAGE VIOLATION</span>
          {activeEpisodes.map((e) => (
            <span key={e.phase}>
              Phase {e.phase} {e.kind} — {e.worst_voltage_v}V (
              {e.kind === "undervoltage" ? "−" : "+"}{e.deviation_v}V vs {nominalV}V nominal), since {fmtTime(e.start_ts)} IST
            </span>
          ))}
          <span className="ml-auto text-[var(--leo-text-dim)]">{violatingCount} buses affected right now · see Actions →</span>
        </div>
      )}
      {!activeOutage && activeEpisodes.length === 0 && activeDrEvent && (
        <div className="rounded-md border border-[var(--leo-accent)] bg-[var(--leo-accent)]/10 px-4 py-3 text-sm flex items-center gap-3">
          <span className="font-semibold text-[var(--leo-accent)]">DR EVENT LIVE</span>
          <span>
            Phase {activeDrEvent.phase}, target {activeDrEvent.target_kw.toFixed(1)}kW — {activeDrEvent.n_sent} offers sent,{" "}
            {activeDrEvent.n_accepted} accepted, {activeDrEvent.n_holdout} held out for verification.
          </span>
        </div>
      )}

      <div className="flex-1 min-h-0 flex gap-4">
        <div className="flex-1 min-w-0">
          <FeederMap
            dtId={DT_ID}
            runId={activeRun?.run_id}
            ts={currentTs ?? undefined}
            backupBusIds={backupBusIds}
            liveDispatchByPhase={liveDispatch}
            onViolatingCount={setViolatingCount}
          />
        </div>

        {/* Actions sidebar: every open DISCOM recommendation for this
            run, live. This is the whole Approve/Acknowledge/Dispatch
            workflow that exists today — there is no Reject path yet
            (confirmed against the schema and API, not just the UI), so
            this doesn't pretend to have one. */}
        <aside className="w-80 shrink-0 flex flex-col gap-3 overflow-y-auto">
          <h2 className="text-sm font-medium text-[var(--leo-text-dim)]">Actions ({openRecs.length})</h2>
          {openRecs.length === 0 && (
            <p className="text-sm text-[var(--leo-text-dim)]">No open recommendations for this run.</p>
          )}
          {openRecs.map((r) => (
            <RecommendationCard key={r.rec_id} runId={activeRun!.run_id} rec={r} onAdvance={() => loadRecs(activeRun!.run_id)} />
          ))}
          {recs.filter((r) => r.status === "resolved").length > 0 && (
            <>
              <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mt-2">Resolved</h2>
              {recs.filter((r) => r.status === "resolved").map((r) => (
                <div key={r.rec_id} className="opacity-60">
                  <RecommendationCard runId={activeRun!.run_id} rec={r} />
                </div>
              ))}
            </>
          )}
        </aside>
      </div>

      {activeRun && (
        <div className="flex items-stretch gap-3 text-xs shrink-0">
          {PHASES.map((p) => {
            const v = liveVoltage[p];
            const d = liveDispatch[p];
            const violating = v && Math.abs(v.voltage_v - nominalV) / nominalV > 0.06;
            return (
              <div
                key={p}
                className="flex-1 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-3 py-2 flex items-center gap-3"
              >
                <span className="font-semibold" style={{ color: PHASE_TEXT_COLOR[p] }}>
                  {p}
                </span>
                <span className={violating ? "text-[var(--leo-bad)]" : "text-[var(--leo-text)]"}>
                  {v && v.supply_present ? `${v.voltage_v.toFixed(1)}V far-end` : v ? "no supply" : "—"}
                </span>
                <span className="text-[var(--leo-text-dim)]">
                  {d
                    ? `batt ${d.actual_kw >= 0 ? "discharge" : "charge"} ${Math.abs(d.actual_kw).toFixed(1)}kW · SoC ${(d.soc_after * 100).toFixed(0)}%`
                    : "—"}
                </span>
                {d?.rule_triggered && (
                  <span className="ml-auto rounded-full bg-[var(--leo-warn)]/20 text-[var(--leo-warn)] px-2 py-0.5">
                    {d.rule_triggered}
                  </span>
                )}
              </div>
            );
          })}
          <div className="flex-1 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] px-3 py-2 flex items-center">
            <span className="text-[var(--leo-text-dim)]">
              {lastEvent
                ? `last event: ${lastEvent.kind} at ${fmtTime(lastEvent.ts)}`
                : "no events yet"}
            </span>
          </div>
        </div>
      )}

      {activeRun && (
        <Timeline
          key={activeRun.run_id}
          runId={activeRun.run_id}
          startTs={activeRun.sim_start}
          endTs={activeRun.sim_end}
          segments={[{ kind: "recorded", startTs: activeRun.sim_start, endTs: activeRun.sim_end }]}
          events={events}
          initialTs={activeRun.sim_start}
          onScrub={setCurrentTs}
        />
      )}
      {!activeRun && runsError && (
        <div role="alert" className="flex items-center gap-3 text-sm text-[var(--leo-bad)]">
          <span>Couldn&apos;t load runs from the cloud API.</span>
          <button onClick={loadRuns} className="underline">
            Retry
          </button>
        </div>
      )}
      {!activeRun && !runsError && (
        <p className="text-sm text-[var(--leo-text-dim)]">
          No recorded run yet — run <code>python -m sim.loop</code> to produce one.
        </p>
      )}
    </main>
  );
}
