"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import RunSelect, { LoadState, getJSON, useRunParam } from "@/components/operator/RunSelect";
import { formatRupees } from "@/lib/format";

const RUNS = ["normal", "load_shedding", "outage", "surplus"];

type LedgerRow = {
  household_id: string; date: string; entry_type: string; deficit_kwh: number | null;
  matched_kwh: number | null; amount_paise: number; payout_scaling_factor: number;
  linked_event_id: string | null; running_balance_paise: number;
};
type LedgerResponse = {
  run_id: string;
  by_stream: { entry_type: string; n_entries: number; total_paise: string }[];
  total_paise: number;
  n_households_paid: number;
  rows: LedgerRow[];
};

const STREAMS: { key: string; label: string; note: string }[] = [
  { key: "dr_incentive", label: "Demand response", note: "Per event, for pumps moved, ACs eased and SMS offers kept. Paid first." },
  { key: "absorption_payment", label: "Solar soaked up", note: "For rooftop solar the batteries stored instead of it being exported." },
  { key: "discharge_rebate", label: "Evening rebate", note: "A share of the battery's evening discharge, for households that took part." },
  { key: "backup_fee", label: "Backup power", note: "Charged to critical premises for power during an outage." },
];

export default function SettlementPanel() {
  const [run, setRun] = useRunParam(RUNS);
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    setLedger(null);
    try {
      setLedger(await getJSON<LedgerResponse>(`/api/ledger/${run}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run]);
  useEffect(() => { load(); }, [load]);

  const streams = useMemo(() => {
    if (!ledger) return [];
    return STREAMS.map((s) => {
      const rows = ledger.rows.filter((r) => r.entry_type === s.key);
      const funded = rows.length ? Math.min(...rows.map((r) => r.payout_scaling_factor)) : null;
      return { ...s, n: rows.length, paid: rows.reduce((a, r) => a + r.amount_paise, 0), funded };
    }).filter((s) => s.n > 0);
  }, [ledger]);

  const households = useMemo(() => {
    if (!ledger) return [];
    const by = new Map<string, Record<string, number>>();
    for (const r of ledger.rows) {
      const h = by.get(r.household_id) ?? {};
      h[r.entry_type] = (h[r.entry_type] ?? 0) + r.amount_paise;
      by.set(r.household_id, h);
    }
    return [...by.entries()]
      .map(([id, amounts]) => ({ id, amounts, total: Object.values(amounts).reduce((a, b) => a + b, 0) }))
      .sort((a, b) => b.total - a.total || a.id.localeCompare(b.id));
  }, [ledger]);

  const FEE = "backup_fee";
  const payout = ledger ? ledger.rows.filter((r) => r.entry_type !== FEE).reduce((a, r) => a + r.amount_paise, 0) : 0;
  const fees = ledger ? ledger.rows.filter((r) => r.entry_type === FEE).reduce((a, r) => a + r.amount_paise, 0) : 0;
  const nPaid = households.filter((h) => Object.entries(h.amounts).some(([k, v]) => k !== FEE && v > 0)).length;
  const unfunded = streams.filter((s) => s.funded === 0);
  const cols = streams.map((s) => s.key);
  const shown = showAll ? households : households.slice(0, 20);

  return (
    <main id="main-content" className="mx-auto w-full max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold leading-tight">Settlement</h1>
          <p className="mt-1 max-w-[72ch] text-sm text-[var(--leo-text-dim)]">
            What each household is paid for the day, worked out the next morning once meter data arrives. Payments are sized from what the
            operator actually earned, so the ledger never promises more than it can pay.
          </p>
        </div>
        <RunSelect runs={RUNS} value={run} onChange={(r) => { setRun(r); setShowAll(false); }} />
      </div>

      <LoadState error={error} loading={!error && !ledger} onRetry={load} />

      {ledger && ledger.rows.length === 0 && (
        <p className="mt-8 text-sm text-[var(--leo-text-dim)]">No payments were recorded for this day.</p>
      )}

      {ledger && ledger.rows.length > 0 && (
        <>
          <section aria-labelledby="totals" className="mt-8">
            <h2 id="totals" className="sr-only">Totals</h2>
            <dl className="flex flex-wrap gap-x-12 gap-y-4">
              <div className="flex flex-col-reverse">
                <dt className="text-[13px] text-[var(--leo-text-dim)]">paid to households</dt>
                <dd className="text-3xl font-semibold">{formatRupees(payout)}</dd>
              </div>
              <div className="flex flex-col-reverse">
                <dt className="text-[13px] text-[var(--leo-text-dim)]">households paid</dt>
                <dd className="text-3xl font-semibold">{nPaid}</dd>
              </div>
              {fees > 0 && (
                <div className="flex flex-col-reverse">
                  <dt className="text-[13px] text-[var(--leo-text-dim)]">backup fees collected</dt>
                  <dd className="text-3xl font-semibold">{formatRupees(fees)}</dd>
                </div>
              )}
            </dl>

            <div className="mt-6 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-[13px] text-[var(--leo-text-dim)]">
                  <tr className="border-b border-[var(--leo-border)]">
                    <th className="py-2 pr-4 font-normal">Payment</th>
                    <th className="py-2 pr-4 text-right font-normal">Entries</th>
                    <th className="py-2 pr-4 text-right font-normal">Amount</th>
                    <th className="py-2 pr-4 text-right font-normal">Share of claims paid</th>
                    <th className="py-2 font-normal">What it is for</th>
                  </tr>
                </thead>
                <tbody>
                  {streams.map((s) => (
                    <tr key={s.key} className="border-b border-[var(--leo-border)]">
                      <td className="py-2.5 pr-4 font-medium">{s.label}</td>
                      <td className="py-2.5 pr-4 text-right">{s.n}</td>
                      <td className="py-2.5 pr-4 text-right">{formatRupees(s.paid)}</td>
                      <td className={`py-2.5 pr-4 text-right ${s.funded === 0 ? "text-[var(--leo-warn)]" : ""}`}>
                        {s.funded != null ? `${(s.funded * 100).toFixed(0)}%` : "—"}
                      </td>
                      <td className="py-2.5 text-[var(--leo-text-dim)]">{s.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {unfunded.length > 0 && (
              <p className="mt-3 max-w-[72ch] text-sm text-[var(--leo-text-dim)]">
                {unfunded.map((s, i) => (i ? s.label.toLowerCase() : s.label)).join(" and ")} claims are recorded but paid at 0% today. Demand response is paid first from the
                day&apos;s payout budget, and nothing was left for these. The claims stay on record for each household.
              </p>
            )}
          </section>

          <section aria-labelledby="by-household" className="mt-10">
            <h2 id="by-household" className="text-lg font-semibold">By household</h2>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-[13px] text-[var(--leo-text-dim)]">
                  <tr className="border-b border-[var(--leo-border)]">
                    <th className="py-2 pr-4 font-normal">Household</th>
                    {cols.map((c) => (
                      <th key={c} className="py-2 pr-4 text-right font-normal">{STREAMS.find((s) => s.key === c)?.label}</th>
                    ))}
                    <th className="py-2 text-right font-normal">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((h) => (
                    <tr key={h.id} className="border-b border-[var(--leo-border)]">
                      <td className="py-2 pr-4">{h.id}</td>
                      {cols.map((c) => (
                        <td key={c} className="py-2 pr-4 text-right text-[var(--leo-text-dim)]">{h.amounts[c] != null ? formatRupees(h.amounts[c]) : "—"}</td>
                      ))}
                      <td className="py-2 text-right font-medium">{formatRupees(h.total)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {households.length > 20 && (
              <button type="button" onClick={() => setShowAll((v) => !v)}
                className="mt-3 rounded-md border border-[var(--leo-border)] px-3 py-1.5 text-sm hover:bg-[var(--leo-panel-raised)]">
                {showAll ? "Show the top 20" : `Show all ${households.length} households`}
              </button>
            )}
          </section>
        </>
      )}
    </main>
  );
}
