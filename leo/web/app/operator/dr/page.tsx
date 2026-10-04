"use client";

import { useCallback, useEffect, useState } from "react";
import PhaseChip from "@/components/operator/PhaseChip";
import RunSelect, { LoadState, getJSON, useRunParam } from "@/components/operator/RunSelect";
import { fmtTime } from "@/components/operator/types";

const RUNS = ["normal", "load_shedding", "outage", "surplus"];

type DrOffer = {
  household_id: string; level: number; predicted_kwh: number; sent_at: string | null;
  channel: string; replied: boolean; is_holdout: boolean; verified_kwh: number | null;
};
type DrEvent = {
  event_id: string; phase: string; window_start: string; window_end: string; target_kw: number;
  n_offers: number; n_sent: number; n_accepted: number; n_holdout: number; offers: DrOffer[];
};
type RunEvent = { ts: string; kind: string; payload: Record<string, unknown> };
type Action = { kind: string; title: string; detail: string; meta: Record<string, unknown> };

const fmtSlot = (h: number) => `${String(Math.floor(h)).padStart(2, "0")}:${String(Math.round((h % 1) * 60)).padStart(2, "0")}`;

function Figure({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex flex-col-reverse">
      <dt className="text-[13px] text-[var(--leo-text-dim)]">{label}</dt>
      <dd className="text-2xl font-semibold leading-tight">{value}</dd>
    </div>
  );
}

export default function DrEventsView() {
  const [run, setRun] = useRunParam(RUNS);
  const [events, setEvents] = useState<DrEvent[] | null>(null);
  const [auto, setAuto] = useState<RunEvent[]>([]);
  const [actions, setActions] = useState<Action[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setEvents(null);
    try {
      const [d, ev, al] = await Promise.all([
        getJSON<DrEvent[]>(`/api/dr_events/${run}`), getJSON<RunEvent[]>(`/api/events/${run}`), getJSON<Action[]>(`/api/action_log/${run}`),
      ]);
      setEvents(d);
      setAuto(ev);
      setActions(al);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run]);
  useEffect(() => { load(); }, [load]);

  const pump = auto.find((e) => e.kind === "dr_auto_shift")?.payload as { pump_kwh: number; pump_homes: string[]; pump_slot_ist: number } | undefined;
  const acEv = auto.find((e) => e.kind === "dr_auto_ac");
  const ac = acEv?.payload as { ac_kw: number; ac_kwh: number; ac_homes: string[]; ac_payment_rs: number } | undefined;
  const result = actions.find((a) => a.kind === "dr_result");

  return (
    <main id="main-content" className="mx-auto w-full max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold leading-tight">Demand response</h1>
          <p className="mt-1 max-w-[72ch] text-sm text-[var(--leo-text-dim)]">
            What LEO asked households to do, who did it, and what it saved. Enrolled pumps and ACs respond automatically. Everyone else
            gets an SMS: LEO picks, for each household, whether to simply ask or to offer ₹25, ₹50 or ₹100, based on how that household has
            responded before.
          </p>
        </div>
        <RunSelect runs={RUNS} value={run} onChange={setRun} />
      </div>

      <LoadState error={error} loading={!error && !events} onRetry={load} />

      {events && (
        <>
          {(pump || ac) && (
            <section aria-labelledby="automatic" className="mt-8">
              <h2 id="automatic" className="text-lg font-semibold">Automatic, through enrolled devices</h2>
              <div className="mt-3 grid gap-6 md:grid-cols-2">
                {pump && (
                  <div className="border-t border-[var(--leo-border)] pt-3">
                    <h3 className="font-medium">Irrigation pumps moved to midday</h3>
                    <dl className="mt-2 grid grid-cols-3 gap-4">
                      <Figure value={String(pump.pump_homes.length)} label="pumps" />
                      <Figure value={`${pump.pump_kwh.toFixed(1)} kWh`} label="moved off the evening" />
                      <Figure value={fmtSlot(pump.pump_slot_ist)} label="new start, IST" />
                    </dl>
                    <p className="mt-2 text-[13px] text-[var(--leo-text-dim)]">Smart relays run each pump for an hour on midday solar instead of 18:00–19:00.</p>
                  </div>
                )}
                {ac && acEv && (
                  <div className="border-t border-[var(--leo-border)] pt-3">
                    <h3 className="font-medium">Air conditioners cycled down from {fmtTime(acEv.ts)} IST</h3>
                    <dl className="mt-2 grid grid-cols-3 gap-4">
                      <Figure value={String(ac.ac_homes.length)} label="homes" />
                      <Figure value={`${ac.ac_kw.toFixed(1)} kW`} label="less load, on average" />
                      <Figure value={`₹${ac.ac_payment_rs}`} label="paid to each home" />
                    </dl>
                    <p className="mt-2 text-[13px] text-[var(--leo-text-dim)]">Pre-cooled in the two hours before the event. {ac.ac_kwh.toFixed(1)} kWh in total.</p>
                  </div>
                )}
              </div>
            </section>
          )}

          <section aria-labelledby="sms" className="mt-10">
            <h2 id="sms" className="text-lg font-semibold">SMS offers</h2>
            {events.length === 0 && <p className="mt-2 text-sm text-[var(--leo-text-dim)]">No SMS offers on this day. The forecast did not need them.</p>}
            {events.map((ev) => {
              const sent = ev.offers.filter((o) => !o.is_holdout);
              const byLevel = new Map<number, number>();
              for (const o of sent) byLevel.set(o.level, (byLevel.get(o.level) ?? 0) + 1);
              const verified = sent.reduce((s, o) => s + (o.verified_kwh ?? 0), 0);
              const rate = (ev.n_accepted / Math.max(1, ev.n_sent)) * 100;
              return (
                <article key={ev.event_id} className="mt-3 rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                  <h3 className="flex flex-wrap items-baseline gap-x-3 text-base font-medium">
                    <PhaseChip phase={ev.phase} />
                    <span>{fmtTime(ev.window_start, true)}–{fmtTime(ev.window_end)} IST</span>
                    <span className="text-sm font-normal text-[var(--leo-text-dim)]">aiming for {ev.target_kw.toFixed(1)} kW less load</span>
                  </h3>
                  <dl className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
                    <Figure value={String(ev.n_sent)} label="households messaged" />
                    <Figure value={`${ev.n_accepted} (${rate.toFixed(0)}%)`} label="said yes" />
                    <Figure value={String(ev.n_holdout)} label="held back to measure against" />
                    <Figure value={verified > 0 ? `${verified.toFixed(1)} kWh` : "next morning"} label="verified saving" />
                  </dl>
                  <p className="mt-3 text-sm">
                    {[...byLevel.entries()].sort((a, b) => a[0] - b[0])
                      .map(([lvl, n]) => (lvl === 0 ? `${n} asked without payment` : `${n} offered ₹${lvl}`)).join(", ")}.
                    {" "}{ev.n_offers} households were chosen: {ev.n_sent} were messaged and {ev.n_holdout} were deliberately not, so the saving can be measured against similar homes.
                  </p>
                  {result && <p className="mt-1 text-sm text-[var(--leo-text-dim)]">{result.title}. {result.detail}</p>}
                </article>
              );
            })}
          </section>
        </>
      )}
    </main>
  );
}
