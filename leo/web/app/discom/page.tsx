"use client";

import { useEffect, useState } from "react";
import RecommendationCard, { Recommendation } from "@/components/recommendations/RecommendationCard";
import { formatRupees } from "@/lib/format";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const RUN_IDS = ["normal", "outage"];
const DFPO_TARGET_SHARE = 0.005; // Karnataka DF/DSM FY26-27 target: 0.5% of peak demand

type RecWithRun = Recommendation & { runId: string };
type DrEventSummary = { target_kw: number; n_sent: number; n_accepted: number; n_holdout: number };
type Household = { id: string; is_critical: boolean; critical_class: string };

export default function DiscomDashboard() {
  const [recs, setRecs] = useState<RecWithRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [drEvents, setDrEvents] = useState<DrEventSummary[]>([]);
  const [households, setHouseholds] = useState<Household[]>([]);
  const [ledgerTotal, setLedgerTotal] = useState(0);
  const [loadError, setLoadError] = useState(false);

  const load = async () => {
    const all = await Promise.all(
      RUN_IDS.map((runId) =>
        fetch(`${CLOUD_API_URL}/api/recommendations/${runId}`)
          .then((r) => r.json())
          .then((rows: Recommendation[]) => rows.map((r): RecWithRun => ({ ...r, runId })))
          .catch(() => [] as RecWithRun[])
      )
    );
    setRecs(all.flat());
    setLoading(false);
  };

  const loadAll = () => {
    setLoadError(false);
    load();
    fetch(`${CLOUD_API_URL}/api/dr_events/normal`).then((r) => r.json()).then(setDrEvents).catch(() => setLoadError(true));
    fetch(`${CLOUD_API_URL}/api/households`).then((r) => r.json()).then(setHouseholds).catch(() => setLoadError(true));
    fetch(`${CLOUD_API_URL}/api/ledger/normal`).then((r) => r.json()).then((d) => setLedgerTotal(d.total_paise)).catch(() => setLoadError(true));
  };

  useEffect(() => {
    loadAll();
  }, []);

  const criticalPremises = households.filter((h) => h.is_critical);
  const totalTargetKw = drEvents.reduce((s, e) => s + e.target_kw, 0);
  const totalAccepted = drEvents.reduce((s, e) => s + e.n_accepted, 0);
  const totalHoldout = drEvents.reduce((s, e) => s + e.n_holdout, 0);

  const open = recs.filter((r) => r.status === "open");
  const inProgress = recs.filter((r) => r.status === "acknowledged" || r.status === "dispatched");
  const resolved = recs.filter((r) => r.status === "resolved");

  return (
    <main id="main-content" className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold">DISCOM dashboard</h1>
      <p className="text-sm text-[var(--leo-text-dim)] mb-6">
        Action queue: acknowledge → dispatched → resolved. This is the only place LEO&apos;s
        control boundary becomes visible — the operator recommends, the DISCOM decides.
      </p>

      {loadError && (
        <div role="alert" className="mb-4 flex items-center justify-between gap-3 rounded-md bg-[var(--leo-bad)]/15 text-[var(--leo-bad)] text-sm px-3 py-2">
          <span>Some reporting data failed to load.</span>
          <button onClick={loadAll} className="underline shrink-0">
            Retry
          </button>
        </div>
      )}

      {loading && <p className="text-sm text-[var(--leo-text-dim)]">Loading…</p>}

      {!loading && recs.length === 0 && (
        <p className="text-sm text-[var(--leo-text-dim)]">
          No open recommendations — run <code>python -m cloud.recommendations</code> after a
          recorded run to generate some.
        </p>
      )}

      {open.length > 0 && (
        <section className="mb-6">
          <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-2">Open ({open.length})</h2>
          <div className="flex flex-col gap-3">
            {open.map((r) => (
              <RecommendationCard key={r.rec_id} runId={r.runId} rec={r} onAdvance={load} />
            ))}
          </div>
        </section>
      )}

      {inProgress.length > 0 && (
        <section className="mb-6">
          <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-2">
            In progress ({inProgress.length})
          </h2>
          <div className="flex flex-col gap-3">
            {inProgress.map((r) => (
              <RecommendationCard key={r.rec_id} runId={r.runId} rec={r} onAdvance={load} />
            ))}
          </div>
        </section>
      )}

      {resolved.length > 0 && (
        <section className="mb-10">
          <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-2">
            Resolved ({resolved.length})
          </h2>
          <div className="flex flex-col gap-3 opacity-60">
            {resolved.map((r) => (
              <RecommendationCard key={r.rec_id} runId={r.runId} rec={r} />
            ))}
          </div>
        </section>
      )}

      <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3 mt-4">Reporting (static)</h2>
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
          <p className="text-xs text-[var(--leo-text-dim)]">Verified flexibility vs holdout</p>
          <p className="text-xl font-semibold">{totalTargetKw.toFixed(1)} kW target</p>
          <p className="text-xs text-[var(--leo-text-dim)] mt-1">
            {totalAccepted} accepted · {totalHoldout} held out for programme-level verification
          </p>
        </div>
        <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
          <p className="text-xs text-[var(--leo-text-dim)]">DFPO progress</p>
          <p className="text-xl font-semibold">{formatRupees(ledgerTotal)} settled</p>
          <p className="text-xs text-[var(--leo-text-dim)] mt-1">
            target: {(DFPO_TARGET_SHARE * 100).toFixed(1)}% of peak demand (Karnataka FY26-27)
          </p>
        </div>
        <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
          <p className="text-xs text-[var(--leo-text-dim)]">Registered critical premises</p>
          <p className="text-xl font-semibold">{criticalPremises.length}</p>
          <p className="text-xs text-[var(--leo-text-dim)] mt-1">
            {criticalPremises.map((h) => h.critical_class).join(", ") || "none registered"}
          </p>
        </div>
        <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
          <p className="text-xs text-[var(--leo-text-dim)]">Planning insight</p>
          <p className="text-sm mt-1">
            Phase Y sags below the voltage floor (down to ~216V on a 250V nominal) for most of the
            day — a heavily loaded phase on an undersized transformer. LEO&apos;s 7.5kW battery and DR
            offers can&apos;t close a gap that size locally; see the open recommendation above (raise tap).
          </p>
        </div>
      </div>
    </main>
  );
}
