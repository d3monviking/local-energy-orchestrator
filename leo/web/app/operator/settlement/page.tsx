"use client";

import { useEffect, useState } from "react";
import { formatRupees } from "@/lib/format";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type LedgerResponse = {
  run_id: string;
  by_stream: { entry_type: string; n_entries: number; total_paise: string }[];
  total_paise: number;
  n_households_paid: number;
  rows: {
    household_id: string; date: string; entry_type: string; deficit_kwh: number | null;
    matched_kwh: number | null; amount_paise: number; payout_scaling_factor: number;
    linked_event_id: string | null; running_balance_paise: number;
  }[];
};

const ENTRY_LABEL: Record<string, string> = {
  dr_incentive: "DR incentive", absorption_payment: "Stream 1 — absorption",
  discharge_rebate: "Stream 2 — rebate", backup_fee: "Backup fee",
};

export default function SettlementPanel() {
  const [normal, setNormal] = useState<LedgerResponse | null>(null);
  const [outage, setOutage] = useState<LedgerResponse | null>(null);

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/ledger/normal`).then((r) => r.json()).then(setNormal).catch(() => {});
    fetch(`${CLOUD_API_URL}/api/ledger/outage`).then((r) => r.json()).then(setOutage).catch(() => {});
  }, []);

  const scalingFactor = normal?.rows[0]?.payout_scaling_factor;

  return (
    <main id="main-content" className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold">Settlement</h1>
      <p className="text-sm text-[var(--leo-text-dim)] mb-6">
        Payouts are sized backwards from realised revenue — the ledger can never promise more
        than the operator earned (§12.2). Unit economics are a query against these rows, not a
        separate model.
      </p>

      {normal && (
        <section className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 mb-4">
          <h2 className="text-sm font-medium mb-3">Day settlement — &apos;normal&apos; run</h2>
          <div className="grid grid-cols-3 gap-3 mb-4 text-center">
            <div>
              <p className="text-xl font-semibold">{formatRupees(normal.total_paise)}</p>
              <p className="text-xs text-[var(--leo-text-dim)]">total paid</p>
            </div>
            <div>
              <p className="text-xl font-semibold">{normal.n_households_paid}</p>
              <p className="text-xs text-[var(--leo-text-dim)]">households paid</p>
            </div>
            <div>
              <p className="text-xl font-semibold">{scalingFactor != null ? `${(scalingFactor * 100).toFixed(0)}%` : "—"}</p>
              <p className="text-xs text-[var(--leo-text-dim)]">of claims funded</p>
            </div>
          </div>
          {normal.by_stream.map((s) => (
            <div key={s.entry_type} className="flex justify-between text-sm border-t border-[var(--leo-border)] py-2">
              <span>{ENTRY_LABEL[s.entry_type] ?? s.entry_type} ({s.n_entries})</span>
              <span>{formatRupees(s.total_paise)}</span>
            </div>
          ))}
        </section>
      )}

      {outage && (
        <section className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 mb-4">
          <h2 className="text-sm font-medium mb-3">Outage day — backup fees</h2>
          {outage.by_stream.map((s) => (
            <div key={s.entry_type} className="flex justify-between text-sm">
              <span>{ENTRY_LABEL[s.entry_type] ?? s.entry_type} ({s.n_entries})</span>
              <span>{formatRupees(s.total_paise)}</span>
            </div>
          ))}
        </section>
      )}

      {normal && (
        <section>
          <h2 className="text-sm font-medium text-[var(--leo-text-dim)] mb-2">Per-household drill-down</h2>
          <table className="w-full text-xs">
            <thead className="text-[var(--leo-text-dim)]">
              <tr>
                <th className="text-left font-normal">Household</th>
                <th className="text-left font-normal">Stream</th>
                <th className="text-left font-normal">Matched kWh</th>
                <th className="text-left font-normal">Amount</th>
                <th className="text-left font-normal">Balance</th>
              </tr>
            </thead>
            <tbody>
              {normal.rows.slice(0, 25).map((r, i) => (
                <tr key={i} className="border-t border-[var(--leo-border)]">
                  <td className="py-1">{r.household_id}</td>
                  <td>{ENTRY_LABEL[r.entry_type] ?? r.entry_type}</td>
                  <td>{r.matched_kwh?.toFixed(3) ?? "—"}</td>
                  <td>{formatRupees(r.amount_paise)}</td>
                  <td>{formatRupees(r.running_balance_paise)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {normal.rows.length > 25 && (
            <p className="text-xs text-[var(--leo-text-dim)] mt-2">+ {normal.rows.length - 25} more rows</p>
          )}
        </section>
      )}
    </main>
  );
}
