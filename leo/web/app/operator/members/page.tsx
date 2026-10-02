"use client";

import { useEffect, useState } from "react";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type Household = {
  id: string; phase: string; sanctioned_load_kw: number; has_pv: boolean;
  is_business: boolean; is_critical: boolean; critical_class: string;
};

export default function MembersView() {
  const [households, setHouseholds] = useState<Household[]>([]);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/households`).then((r) => r.json()).then(setHouseholds).catch(() => {});
  }, []);

  const filtered = households.filter((h) => h.id.toLowerCase().includes(filter.toLowerCase()));

  return (
    <main id="main-content" className="p-6 max-w-3xl">
      <h1 className="text-lg font-semibold">Members</h1>
      <p className="text-sm text-[var(--leo-text-dim)] mb-4">
        {households.length} registered households. Consent state lives per-household on the
        citizen app; device health has no live mock IoT feed yet, so registration status stands
        in for it here.
      </p>

      <label htmlFor="member-filter" className="sr-only">
        Filter by household id
      </label>
      <input
        id="member-filter"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by household id…"
        className="w-full mb-4 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-2 text-sm"
      />

      <table className="w-full text-sm">
        <thead className="text-[var(--leo-text-dim)] text-xs">
          <tr>
            <th className="text-left font-normal py-2">Household</th>
            <th className="text-left font-normal">Phase</th>
            <th className="text-left font-normal">Sanctioned kW</th>
            <th className="text-left font-normal">PV</th>
            <th className="text-left font-normal">Business</th>
            <th className="text-left font-normal">Critical</th>
          </tr>
        </thead>
        <tbody>
          {filtered.slice(0, 40).map((h) => (
            <tr key={h.id} className="border-t border-[var(--leo-border)]">
              <td className="py-1.5">{h.id}</td>
              <td>{h.phase}</td>
              <td>{h.sanctioned_load_kw.toFixed(2)}</td>
              <td>{h.has_pv ? "yes" : "—"}</td>
              <td>{h.is_business ? "yes" : "—"}</td>
              <td>{h.is_critical ? h.critical_class : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {filtered.length > 40 && (
        <p className="text-xs text-[var(--leo-text-dim)] mt-2">+ {filtered.length - 40} more</p>
      )}
    </main>
  );
}
