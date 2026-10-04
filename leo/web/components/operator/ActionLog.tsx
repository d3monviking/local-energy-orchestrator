"use client";

/**
 * Everything LEO (and the network, and the DISCOM) did, in time order,
 * each with the reason it was done. Items before the playhead are
 * "done", items spanning it are "now", items after it are "upcoming" —
 * the day-ahead ones (forecast, plan approval, DR offers) sit before the
 * day starts, which is the point: LEO acts before the event, not after.
 */

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { ActionItem, fmtTime, ms } from "./types";

const ACTOR_COLOR: Record<string, string> = {
  Forecast: "#9b7fe0", Operator: "#3ba9ff", "DR engine": "#2fbf71", Network: "#e0473e",
  Sensors: "#e0473e", DISCOM: "#3ba9ff", LEO: "#2fbf71", Escalation: "#e0a72e", "Backup (C9)": "#e0a72e",
};
const actorColor = (a: string) => ACTOR_COLOR[a] ?? (a.startsWith("Battery") ? "#3ba9ff" : a.startsWith("Mode") ? "#e0a72e" : "#93a1b0");

const FILTERS: { key: string; label: string; match: (a: ActionItem) => boolean }[] = [
  { key: "all", label: "All", match: () => true },
  { key: "plan", label: "Forecast & plan", match: (a) => ["forecast", "plan", "off"].includes(a.kind) },
  { key: "battery", label: "Battery", match: (a) => a.kind.startsWith("battery_") },
  { key: "dr", label: "DR", match: (a) => a.kind.startsWith("dr_") },
  { key: "network", label: "Network", match: (a) => a.actor === "Network" },
  { key: "outage", label: "Outage", match: (a) => ["load_shedding_notice", "alert_sent", "grid_loss", "grid_return", "backup_start", "backup_end", "restore", "mode"].includes(a.kind) },
  { key: "esc", label: "Escalations", match: (a) => a.kind === "recommendation" },
];

export default function ActionLog({ items, currentTs, startTs, playing, onSeek }: {
  items: ActionItem[]; currentTs: string | null; startTs: string; playing: boolean; onSeek: (ts: string) => void;
}) {
  const [filter, setFilter] = useState("all");
  const listRef = useRef<HTMLOListElement | null>(null);
  const now = currentTs ? ms(currentTs) : ms(startTs);
  const shown = useMemo(() => items.filter(FILTERS.find((f) => f.key === filter)!.match), [items, filter]);
  const lastPastIdx = shown.reduce((acc, a, i) => (ms(a.ts) <= now ? i : acc), -1);

  useEffect(() => {
    if (!playing || lastPastIdx < 0) return;
    listRef.current?.children[lastPastIdx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [lastPastIdx, playing]);

  return (
    <div className="flex flex-col gap-2 min-h-0 h-full">
      <div>
        <h2 className="text-sm font-semibold">What LEO did, and why</h2>
        <p className="text-xs text-[var(--leo-text-dim)]">Click any entry to jump the timeline to it.</p>
      </div>
      <div className="flex flex-wrap gap-1">
        {FILTERS.map((f) => (
          <button key={f.key} onClick={() => setFilter(f.key)}
            className={`text-[11px] rounded-full px-2 py-0.5 border ${filter === f.key
              ? "bg-[var(--leo-accent)] text-black border-[var(--leo-accent)]"
              : "border-[var(--leo-border)] text-[var(--leo-text-dim)]"}`}>
            {f.label}
          </button>
        ))}
      </div>
      <ol ref={listRef} className="flex flex-col gap-1.5 overflow-y-auto min-h-0 pr-1">
        {shown.map((a, i) => {
          const t = ms(a.ts);
          const end = a.end ? ms(a.end) : t;
          const state = t > now ? "upcoming" : now <= end && a.end ? "now" : "done";
          const beforeDay = t < ms(startTs);
          return (
            <li key={i}>
              <button onClick={() => onSeek(beforeDay ? startTs : a.ts)}
                className={`w-full text-left rounded-md border px-2.5 py-2 ${state === "now"
                  ? "border-[var(--leo-accent)] bg-[var(--leo-accent)]/10"
                  : "border-[var(--leo-border)] bg-[var(--leo-panel)]"} ${state === "upcoming" ? "opacity-45" : ""}`}>
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="tabular-nums text-[var(--leo-text-dim)]">
                    {fmtTime(a.ts, beforeDay)}{a.end && a.end !== a.ts ? `–${fmtTime(a.end)}` : ""}
                  </span>
                  <span className="rounded px-1.5 font-medium" style={{ background: `${actorColor(a.actor)}26`, color: actorColor(a.actor) }}>
                    {a.actor}
                  </span>
                  {beforeDay && <span className="text-[var(--leo-text-dim)]">day before</span>}
                  {state === "now" && <span className="ml-auto font-semibold text-[var(--leo-accent)]">NOW</span>}
                  {state === "upcoming" && <span className="ml-auto text-[var(--leo-text-dim)]">upcoming</span>}
                </div>
                <p className={`text-[13px] mt-0.5 ${a.severity === "bad" ? "text-[var(--leo-bad)]" : a.severity === "warn" ? "text-[var(--leo-warn)]" : ""}`}>
                  {a.title}
                </p>
                {a.detail && <p className="text-xs text-[var(--leo-text-dim)] mt-0.5">{a.detail}</p>}
                {a.reason && <p className="text-xs mt-0.5"><span className="text-[var(--leo-text-dim)]">Why: </span>{a.reason}</p>}
                {a.link && (
                  <Link href={a.link} onClick={(e) => e.stopPropagation()}
                    className="inline-block text-xs text-[var(--leo-accent)] mt-0.5 hover:underline">
                    Details →
                  </Link>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
