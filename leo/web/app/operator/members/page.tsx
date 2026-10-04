"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import PhaseChip from "@/components/operator/PhaseChip";
import { LoadState, getJSON } from "@/components/operator/RunSelect";

type Household = {
  id: string; phase: string; sanctioned_load_kw: number; has_pv: boolean;
  is_business: boolean; is_critical: boolean; critical_class: string;
};

const PAGE = 50;

export default function MembersView() {
  const [households, setHouseholds] = useState<Household[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState("all");
  const [kind, setKind] = useState("all");
  const [limit, setLimit] = useState(PAGE);

  const load = useCallback(async () => {
    setError(null);
    try {
      setHouseholds(await getJSON<Household[]>("/api/households"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => (households ?? []).filter((h) =>
    h.id.toLowerCase().includes(query.trim().toLowerCase()) &&
    (phase === "all" || h.phase === phase) &&
    (kind === "all" || (kind === "critical" && h.is_critical) || (kind === "business" && h.is_business) || (kind === "pv" && h.has_pv))
  ), [households, query, phase, kind]);

  const n = households?.length ?? 0;
  const count = (f: (h: Household) => boolean) => (households ?? []).filter(f).length;

  return (
    <main id="main-content" className="mx-auto w-full max-w-[1180px] px-6 pb-16 pt-6">
      <h1 className="text-[22px] font-semibold leading-tight">Members</h1>
      <p className="mt-1 max-w-[72ch] text-sm text-[var(--leo-text-dim)]">
        {households
          ? `${n} households on this transformer: ${count((h) => h.has_pv)} with rooftop solar, ${count((h) => h.is_business)} businesses, ${count((h) => h.is_critical)} critical premises kept powered in outages.`
          : "Households on this transformer."}{" "}
        Device health is not shown yet: the prototype has no live device feed.
      </p>

      <LoadState error={error} loading={!error && !households} onRetry={load} />

      {households && (
        <>
          <div className="mt-6 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-[13px] text-[var(--leo-text-dim)]">
              Household
              <input value={query} onChange={(e) => { setQuery(e.target.value); setLimit(PAGE); }} placeholder="e.g. HH-014"
                className="w-48 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-1.5 text-sm text-[var(--leo-text)]" />
            </label>
            <label className="flex flex-col gap-1 text-[13px] text-[var(--leo-text-dim)]">
              Phase
              <select value={phase} onChange={(e) => { setPhase(e.target.value); setLimit(PAGE); }}
                className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-2.5 py-1.5 text-sm text-[var(--leo-text)]">
                <option value="all">All phases</option>
                <option value="R">R</option><option value="Y">Y</option><option value="B">B</option>
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[13px] text-[var(--leo-text-dim)]">
              Show
              <select value={kind} onChange={(e) => { setKind(e.target.value); setLimit(PAGE); }}
                className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-2.5 py-1.5 text-sm text-[var(--leo-text)]">
                <option value="all">Everyone</option>
                <option value="critical">Critical premises</option>
                <option value="business">Businesses</option>
                <option value="pv">With rooftop solar</option>
              </select>
            </label>
            <p className="ml-auto text-sm text-[var(--leo-text-dim)]" aria-live="polite">{filtered.length} shown</p>
          </div>

          {filtered.length === 0 ? (
            <p className="mt-6 text-sm text-[var(--leo-text-dim)]">No household matches these filters.</p>
          ) : (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-[13px] text-[var(--leo-text-dim)]">
                  <tr className="border-b border-[var(--leo-border)]">
                    <th className="py-2 pr-4 font-normal">Household</th>
                    <th className="py-2 pr-4 font-normal">Phase</th>
                    <th className="py-2 pr-4 text-right font-normal">Sanctioned load</th>
                    <th className="py-2 pr-4 font-normal">Rooftop solar</th>
                    <th className="py-2 pr-4 font-normal">Type</th>
                    <th className="py-2 font-normal">Critical premise</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, limit).map((h) => (
                    <tr key={h.id} className="border-b border-[var(--leo-border)]">
                      <td className="py-2 pr-4">{h.id}</td>
                      <td className="py-2 pr-4"><PhaseChip phase={h.phase} label={false} /></td>
                      <td className="py-2 pr-4 text-right">{h.sanctioned_load_kw.toFixed(1)} kW</td>
                      <td className="py-2 pr-4">{h.has_pv ? "Yes" : "—"}</td>
                      <td className="py-2 pr-4">{h.is_business ? "Business" : "Home"}</td>
                      <td className="py-2">{h.is_critical ? h.critical_class.replace(/_/g, " ") : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {filtered.length > limit && (
            <button type="button" onClick={() => setLimit((l) => l + PAGE)}
              className="mt-3 rounded-md border border-[var(--leo-border)] px-3 py-1.5 text-sm hover:bg-[var(--leo-panel-raised)]">
              Show {Math.min(PAGE, filtered.length - limit)} more
            </button>
          )}
        </>
      )}
    </main>
  );
}
