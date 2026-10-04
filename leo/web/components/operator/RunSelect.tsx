"use client";

import { useEffect, useState } from "react";

export const RUN_LABEL: Record<string, string> = {
  normal: "Peak day, 27 Apr, with LEO",
  baseline: "Peak day, 27 Apr, without LEO",
  load_shedding: "Announced load shedding, 27 Apr",
  outage: "Unplanned outage, 27 Apr",
  surplus: "Sunny surplus day, 11 Feb, with LEO",
  surplus_baseline: "Sunny surplus day, 11 Feb, without LEO",
};

/** The selected recording, kept in ?run= so a view can be linked to and reloaded. */
export function useRunParam(allowed: string[], fallback = allowed[0]) {
  const [run, setRun] = useState(fallback);
  useEffect(() => {
    const r = new URLSearchParams(window.location.search).get("run");
    if (r && allowed.includes(r)) setRun(r);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const set = (r: string) => {
    setRun(r);
    const u = new URL(window.location.href);
    u.searchParams.set("run", r);
    window.history.replaceState(null, "", u);
  };
  return [run, set] as const;
}

export default function RunSelect({ runs, value, onChange }: { runs: string[]; value: string; onChange: (r: string) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-[var(--leo-text-dim)]">Recording</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-2.5 py-1.5 text-sm"
      >
        {runs.map((r) => (
          <option key={r} value={r}>{RUN_LABEL[r] ?? r}</option>
        ))}
      </select>
    </label>
  );
}

/** Loading and failure states shared by the operator subpages. */
export function LoadState({ error, loading, onRetry }: { error: string | null; loading: boolean; onRetry: () => void }) {
  if (error)
    return (
      <div role="alert" className="mt-6 rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 text-sm">
        <p className="font-medium text-[var(--leo-bad)]">This page could not load its data.</p>
        <p className="mt-1 text-[var(--leo-text-dim)]">{error}. Check that the cloud API is running, then try again.</p>
        <button type="button" onClick={onRetry} className="mt-3 rounded-md border border-[var(--leo-border)] px-3 py-1.5 hover:bg-[var(--leo-panel-raised)]">Try again</button>
      </div>
    );
  if (loading) return <p className="mt-8 text-sm text-[var(--leo-text-dim)]">Loading…</p>;
  return null;
}

export async function getJSON<T>(path: string): Promise<T> {
  const base = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
  const r = await fetch(`${base}${path}`);
  if (!r.ok) throw new Error(`${path} returned ${r.status}`);
  return r.json();
}
