"use client";

import { useEffect, useMemo, useState } from "react";
import FeederMap from "@/components/map/FeederMap";
import Timeline, { TimelineEventKind, TimelineEventMarker } from "@/components/timeline/Timeline";

const DT_ID = process.env.NEXT_PUBLIC_DT_ID ?? "DT-0417";
const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type RunInfo = { run_id: string; leo_enabled: boolean; sim_start: string; sim_end: string };
type DispatchRow = { block_id: string; ts_end: string; setpoint_kw: number; actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null };
type SensorRow = { sensor_id: string; ts_end: string; voltage_v: number; supply_present: boolean };
type RawEvent = { ts: string; kind: string };

const EVENT_KIND_MAP: Record<string, TimelineEventKind> = {
  grid_loss: "outage", power_fail: "outage", backup_start: "outage",
  grid_return: "restoration", restore: "restoration", backup_end: "restoration",
  overload_trip: "violation",
  dr_event_start: "dr_event", dr_event_end: "dr_event",
};

const RUN_LABEL: Record<string, string> = { normal: "With LEO", baseline: "Without LEO", outage: "Outage replay" };
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

  return (
    <main id="main-content" className="p-6 flex flex-col gap-4 h-[calc(100vh-57px)]">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Map + timeline</h1>
          <p className="text-sm text-[var(--leo-text-dim)]">
            {selectedRunId === "outage"
              ? "Event replay: sensors dark, inverters anti-island, backup circuit lights the registered critical premise, staggered re-transfer on restoration."
              : "Feeder by voltage, scrubbing " + (activeRun?.run_id ?? "…") + ". Phase R clears around 20:30–21:15 IST with the battery discharging; the same window still violates without LEO."}
          </p>
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

      <div className="flex-1 min-h-0">
        <FeederMap
          dtId={DT_ID}
          runId={activeRun?.run_id}
          ts={currentTs ?? undefined}
          backupBusIds={backupBusIds}
          liveDispatchByPhase={liveDispatch}
        />
      </div>

      {activeRun && (
        <div className="flex items-stretch gap-3 text-xs shrink-0">
          {PHASES.map((p) => {
            const v = liveVoltage[p];
            const d = liveDispatch[p];
            const violating = v && Math.abs(v.voltage_v - 230) / 230 > 0.06;
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
                ? `last event: ${lastEvent.kind} at ${new Date(lastEvent.ts).toLocaleTimeString("en-IN", { hour12: false })}`
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
