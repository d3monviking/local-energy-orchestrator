"use client";

import { useEffect, useState } from "react";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const RUN_ID = "normal";

type DrOffer = {
  household_id: string; level: number; predicted_kwh: number; sent_at: string | null;
  channel: string; replied: boolean; is_holdout: boolean; verified_kwh: number | null;
};
type DrEvent = {
  event_id: string; phase: string; window_start: string; window_end: string; target_kw: number;
  n_offers: number; n_sent: number; n_accepted: number; n_holdout: number; offers: DrOffer[];
};

export default function DrEventsView() {
  const [events, setEvents] = useState<DrEvent[]>([]);

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/dr_events/${RUN_ID}`).then((r) => r.json()).then(setEvents).catch(() => setEvents([]));
  }, []);

  return (
    <main id="main-content" className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold">DR events</h1>
      <p className="text-sm text-[var(--leo-text-dim)] mb-6">
        Offers sent, replies, holdout marked, verified kWh. The contextual bandit personalises
        the incentive level per household from learned response history (§11.2) — most residential
        households still look alike on day one (persona isn&apos;t an observable feature), but
        business premises are distinguishable immediately and get offered accordingly.
      </p>

      {events.map((ev) => {
        const byLevel = new Map<number, number>();
        for (const o of ev.offers) byLevel.set(o.level, (byLevel.get(o.level) ?? 0) + 1);

        return (
          <div key={ev.event_id} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 mb-4">
            <div className="flex items-center justify-between mb-3">
              <span className="font-mono text-sm">{ev.event_id}</span>
              <span className="text-xs text-[var(--leo-text-dim)]">phase {ev.phase} · target {ev.target_kw.toFixed(1)}kW</span>
            </div>

            <div className="grid grid-cols-4 gap-3 mb-4 text-center">
              <div>
                <p className="text-xl font-semibold">{ev.n_sent}</p>
                <p className="text-xs text-[var(--leo-text-dim)]">sent</p>
              </div>
              <div>
                <p className="text-xl font-semibold">{ev.n_accepted}</p>
                <p className="text-xs text-[var(--leo-text-dim)]">accepted</p>
              </div>
              <div>
                <p className="text-xl font-semibold">{ev.n_holdout}</p>
                <p className="text-xs text-[var(--leo-text-dim)]">holdout</p>
              </div>
              <div>
                <p className="text-xl font-semibold">{((ev.n_accepted / Math.max(1, ev.n_sent)) * 100).toFixed(0)}%</p>
                <p className="text-xs text-[var(--leo-text-dim)]">response rate</p>
              </div>
            </div>

            <p className="text-xs text-[var(--leo-text-dim)] mb-2">Offers by amount (rupees per event)</p>
            <div className="flex gap-2">
              {[0, 25, 50, 100].map((level) => {
                const count = byLevel.get(level) ?? 0;
                const max = Math.max(1, ...byLevel.values());
                return (
                  <div key={level} className="flex-1 text-center">
                    <div className="h-16 flex items-end">
                      <div
                        className="w-full rounded-t"
                        style={{ height: `${(count / max) * 100}%`, background: "var(--leo-accent)", minHeight: count > 0 ? 4 : 0 }}
                      />
                    </div>
                    <p className="text-xs text-[var(--leo-text-dim)] mt-1">{level === 0 ? "appeal" : `₹${level}`} · {count}</p>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {events.length === 0 && <p className="text-sm text-[var(--leo-text-dim)]">No DR events recorded yet.</p>}
    </main>
  );
}
