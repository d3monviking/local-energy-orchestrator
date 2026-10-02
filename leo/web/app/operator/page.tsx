"use client";

import { useEffect, useState } from "react";
import FeederMap from "@/components/map/FeederMap";
import Timeline, { TimelineEventKind, TimelineEventMarker } from "@/components/timeline/Timeline";

const DT_ID = process.env.NEXT_PUBLIC_DT_ID ?? "DT-0417";
const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type RunInfo = { run_id: string; leo_enabled: boolean; sim_start: string; sim_end: string };

const EVENT_KIND_MAP: Record<string, TimelineEventKind> = {
  grid_loss: "outage", power_fail: "outage", backup_start: "outage",
  grid_return: "restoration", restore: "restoration", backup_end: "restoration",
  overload_trip: "violation",
  dr_event_start: "dr_event", dr_event_end: "dr_event",
};

const RUN_LABEL: Record<string, string> = { normal: "With LEO", baseline: "Without LEO", outage: "Outage replay" };

export default function OperatorHome() {
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("normal");
  const [currentTs, setCurrentTs] = useState<string | null>(null);
  const [events, setEvents] = useState<TimelineEventMarker[]>([]);
  const [backupBusIds, setBackupBusIds] = useState<Set<string>>(new Set());
  const [runsError, setRunsError] = useState(false);

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
      .then((rows: { ts: string; kind: string }[]) =>
        setEvents(
          rows
            .filter((r) => r.kind in EVENT_KIND_MAP)
            .map((r) => ({ ts: r.ts, kind: EVENT_KIND_MAP[r.kind], label: r.kind }))
        )
      )
      .catch(() => setEvents([]));

    fetch(`${CLOUD_API_URL}/api/backup_households/${activeRun.run_id}`)
      .then((r) => r.json())
      .then((rows: { household_id: string; bus_id: string }[]) => setBackupBusIds(new Set(rows.map((r) => r.bus_id))))
      .catch(() => setBackupBusIds(new Set()));
  }, [activeRun?.run_id]);

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
        <FeederMap dtId={DT_ID} runId={activeRun?.run_id} ts={currentTs ?? undefined} backupBusIds={backupBusIds} />
      </div>

      {activeRun && (
        <Timeline
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
