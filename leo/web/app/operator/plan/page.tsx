"use client";

import { useEffect, useState } from "react";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const RUN_ID = "normal";

type PlanPhase = {
  phase: string;
  planner: string;
  reserve_kwh: number;
  approved_at: string | null;
  approved_by: string | null;
  intervals: { ts_end: string; setpoint_kw: number; mode: string }[];
};

type DrOffer = {
  household_id: string;
  level: number;
  predicted_kwh: number;
  sent_at: string | null;
  replied: boolean;
  is_holdout: boolean;
  verified_kwh: number | null;
};

type DrEvent = {
  event_id: string;
  phase: string;
  window_start: string;
  window_end: string;
  target_kw: number;
  v_paise_kwh: number;
  n_offers: number;
  n_sent: number;
  n_accepted: number;
  n_holdout: number;
  offers: DrOffer[];
};

function PlanSparkline({ intervals }: { intervals: PlanPhase["intervals"] }) {
  const max = Math.max(1, ...intervals.map((i) => Math.abs(i.setpoint_kw)));
  return (
    <div className="flex items-end h-12 gap-px">
      {intervals.map((iv, i) => {
        const h = Math.max(1, (Math.abs(iv.setpoint_kw) / max) * 48);
        const color = iv.setpoint_kw < 0 ? "var(--leo-recorded)" : "var(--leo-live)";
        return (
          <div
            key={i}
            title={`${iv.ts_end}: ${iv.setpoint_kw.toFixed(1)}kW`}
            style={{ height: h, width: 2, background: iv.setpoint_kw === 0 ? "var(--leo-border)" : color }}
          />
        );
      })}
    </div>
  );
}

export default function PlanReview() {
  const [plan, setPlan] = useState<PlanPhase[]>([]);
  const [drEvents, setDrEvents] = useState<DrEvent[]>([]);
  const [approving, setApproving] = useState<string | null>(null);

  const load = () => {
    fetch(`${CLOUD_API_URL}/api/plan/${RUN_ID}`)
      .then((r) => r.json())
      .then((d) => setPlan(d.phases))
      .catch(() => setPlan([]));
    fetch(`${CLOUD_API_URL}/api/dr_events/${RUN_ID}`)
      .then((r) => r.json())
      .then(setDrEvents)
      .catch(() => setDrEvents([]));
  };

  useEffect(load, []);

  const approve = async (phase: string) => {
    setApproving(phase);
    try {
      await fetch(`${CLOUD_API_URL}/api/plan/${RUN_ID}/${phase}/approve`, { method: "POST" });
      load();
    } finally {
      setApproving(null);
    }
  };

  return (
    <main id="main-content" className="p-6 max-w-4xl flex flex-col gap-8">
      <div>
        <h1 className="text-lg font-semibold">Plan review</h1>
        <p className="text-sm text-[var(--leo-text-dim)]">
          Per-phase battery schedule and DR offers for tomorrow. Approve commits LEO to run
          exactly this plan — the single most important interaction in the demo.
        </p>
      </div>

      <section>
        <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3">Battery split by phase</h2>
        <div className="flex flex-col gap-4">
          {plan.map((p) => (
            <div key={p.phase} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-3">
                  <span className="font-semibold">Phase {p.phase}</span>
                  <span className="text-xs text-[var(--leo-text-dim)]">
                    planner: {p.planner} · reserve {p.reserve_kwh.toFixed(1)}kWh
                  </span>
                </div>
                {p.approved_at ? (
                  <span className="text-xs rounded-full bg-[var(--leo-ok)]/20 text-[var(--leo-ok)] px-2 py-0.5">
                    Approved by {p.approved_by}
                  </span>
                ) : (
                  <button
                    onClick={() => approve(p.phase)}
                    disabled={approving === p.phase}
                    className="text-sm rounded-md bg-[var(--leo-accent)] text-black px-3 py-1 disabled:opacity-50"
                  >
                    {approving === p.phase ? "Approving…" : "Approve"}
                  </button>
                )}
              </div>
              <PlanSparkline intervals={p.intervals} />
              <div className="flex gap-4 mt-1 text-xs text-[var(--leo-text-dim)]">
                <span className="flex items-center gap-1">
                  <span className="inline-block w-2 h-2" style={{ background: "var(--leo-recorded)" }} /> charge
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block w-2 h-2" style={{ background: "var(--leo-live)" }} /> discharge
                </span>
              </div>
            </div>
          ))}
          {plan.length === 0 && <p className="text-sm text-[var(--leo-text-dim)]">No plan recorded yet.</p>}
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-3">DR offers</h2>
        {drEvents.map((ev) => (
          <div key={ev.event_id} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 mb-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-mono text-sm">{ev.event_id}</span>
              <span className="text-xs text-[var(--leo-text-dim)]">
                phase {ev.phase} · {new Date(ev.window_start).toLocaleTimeString("en-IN", { hour12: false })}–
                {new Date(ev.window_end).toLocaleTimeString("en-IN", { hour12: false })} · target{" "}
                {ev.target_kw.toFixed(1)}kW
              </span>
            </div>
            <div className="flex gap-4 text-sm mb-3">
              <span>{ev.n_sent} sent</span>
              <span>{ev.n_accepted} accepted</span>
              <span>{ev.n_holdout} held out</span>
            </div>
            <table className="w-full text-xs">
              <thead className="text-[var(--leo-text-dim)]">
                <tr>
                  <th className="text-left font-normal">Household</th>
                  <th className="text-left font-normal">Level</th>
                  <th className="text-left font-normal">Predicted kWh</th>
                  <th className="text-left font-normal">Verified kWh</th>
                  <th className="text-left font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {ev.offers.slice(0, 15).map((o) => (
                  <tr key={o.household_id} className="border-t border-[var(--leo-border)]">
                    <td className="py-1">{o.household_id}</td>
                    <td>{(o.level * 100).toFixed(0)}%</td>
                    <td>{o.predicted_kwh.toFixed(2)}</td>
                    <td>{o.verified_kwh != null ? o.verified_kwh.toFixed(2) : "—"}</td>
                    <td>
                      {o.is_holdout ? "holdout" : o.replied ? "accepted" : o.sent_at ? "sent" : "skipped"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {ev.offers.length > 15 && (
              <p className="text-xs text-[var(--leo-text-dim)] mt-1">
                + {ev.offers.length - 15} more
              </p>
            )}
          </div>
        ))}
        {drEvents.length === 0 && <p className="text-sm text-[var(--leo-text-dim)]">No DR events recorded yet.</p>}
      </section>
    </main>
  );
}
