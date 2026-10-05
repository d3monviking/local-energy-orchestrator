"use client";

/**
 * During an outage: which critical premises the backup circuit is serving,
 * in priority order, how much power each draws against its limit, and when
 * each goes back onto the grid. Mirrors gateway/outage.py's allocation.
 */

import { useEffect, useState } from "react";
import { criticalIcon } from "@/components/map/icons";
import { ActionItem, ms } from "./types";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type Premise = {
  household_id: string; phase: string; critical_class: string; priority: number; limit_a: number; limit_kw: number;
  intervals: { ts_end: string; kw: number; limit_active: boolean }[];
};

const CLASS_LABEL: Record<string, string> = {
  health: "Health clinic", water: "Water pump", education: "Tuition centre", livelihood: "Shop",
};

export default function BackupPanel({ runId, currentTs, actions, socByPhase }: {
  runId: string; currentTs: string | null; actions: ActionItem[]; socByPhase: Record<string, number | undefined>;
}) {
  const [premises, setPremises] = useState<Premise[]>([]);

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/backup_premises/${runId}`).then((r) => (r.ok ? r.json() : [])).then(setPremises).catch(() => setPremises([]));
  }, [runId]);

  if (!premises.length || !currentTs) return null;
  const t = ms(currentTs);
  const restoredAt = (id: string) => actions.find((a) => a.kind === "restore" && a.title.includes(id) && ms(a.ts) <= t);
  if (premises.every((p) => restoredAt(p.household_id))) return null; // everyone is back on the grid
  const drawAt = (p: Premise) => (p.intervals.find((iv) => ms(iv.ts_end) >= t) ?? p.intervals[p.intervals.length - 1])?.kw ?? 0;

  return (
    <section aria-label="Backup circuit by priority"
      className="pointer-events-auto w-[280px] rounded-md border border-white/10 bg-[rgb(11_15_20/0.92)] p-3 text-[13px] backdrop-blur-sm">
      <h2 className="font-semibold">Backup circuit, served by priority</h2>
      <p className="mb-2 text-xs text-[var(--leo-text-dim)]">
        Each phase battery feeds its own premises. If one runs short, the lowest priority is cut first.
      </p>
      <ol className="space-y-1.5">
        {premises.map((p) => {
          const back = restoredAt(p.household_id);
          const kw = back ? 0 : drawAt(p);
          return (
            <li key={p.household_id} className="flex items-center gap-2">
              <span aria-label={`Priority ${p.priority}`}
                className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
                  p.priority === 1 ? "bg-white text-[#0b0f14]" : p.priority === 2 ? "bg-[#c3ccd6] text-[#0b0f14]" : "border border-[var(--leo-text-dim)] text-[var(--leo-text-dim)]"}`}>
                {p.priority}
              </span>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={criticalIcon(!back)} alt="" width={18} height={18} className="shrink-0" />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{CLASS_LABEL[p.critical_class] ?? p.critical_class} <span className="text-[var(--leo-text-dim)]">· {p.phase}</span></span>
                <span className="mt-0.5 block h-1.5 w-full rounded bg-white/10">
                  <span className="block h-1.5 rounded bg-[var(--leo-battery)]" style={{ width: `${Math.min(100, (kw / p.limit_kw) * 100)}%` }} />
                </span>
              </span>
              <span className="w-[74px] shrink-0 text-right text-xs">
                {back ? <span className="text-[var(--leo-ok)]">back on grid</span> : <>{kw.toFixed(1)} / {p.limit_kw.toFixed(1)} kW</>}
              </span>
            </li>
          );
        })}
      </ol>
      <p className="mt-2 border-t border-white/10 pt-2 text-xs text-[var(--leo-text-dim)]">
        Batteries {["R", "Y", "B"].map((ph) => `${ph} ${socByPhase[ph] != null ? Math.round(socByPhase[ph]! * 100) : "—"}%`).join(" · ")}.
        On restoration, least critical reconnect first; the clinic stays on backup until the grid is stable.
      </p>
    </section>
  );
}
