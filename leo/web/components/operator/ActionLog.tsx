"use client";

/**
 * Everything LEO (and the network, and the DISCOM) did, in time order,
 * each with the reason it was done. Items before the playhead are
 * "done", items spanning it are "now". Ahead of the playhead only what
 * was already planned is listed (battery schedule, DR windows); outcomes
 * appear when the replay reaches them, so the log never shows the future.
 */

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { ActionItem, fmtTime, ms } from "./types";

const FILTERS: { key: string; label: string; match: (a: ActionItem) => boolean }[] = [
  { key: "all", label: "All", match: () => true },
  { key: "plan", label: "Forecast and plan", match: (a) => ["forecast", "plan", "off"].includes(a.kind) },
  { key: "battery", label: "Battery", match: (a) => a.kind.startsWith("battery_") },
  { key: "dr", label: "DR", match: (a) => a.kind.startsWith("dr_") },
  { key: "network", label: "Network", match: (a) => a.actor === "Network" },
  { key: "outage", label: "Outage", match: (a) => ["load_shedding_notice", "alert_sent", "grid_loss", "grid_return", "backup_start", "backup_end", "restore", "mode"].includes(a.kind) },
  { key: "esc", label: "Sent to DISCOM", match: (a) => a.kind === "recommendation" },
];

/** Known in advance, so safe to list before the playhead reaches it. */
const isPlanned = (a: ActionItem) =>
  (a.kind.startsWith("battery_") && a.meta?.source === "plan") || a.kind === "dr_window" || a.kind === "dr_auto_ac";

const actorLabel = (a: string) => a.replace(/\s*\(C\d+\)$/, "").replace(/^Escalation$/, "Sent to DISCOM");

export default function ActionLog({ items, currentTs, startTs, playing, onSeek }: {
  items: ActionItem[]; currentTs: string | null; startTs: string; playing: boolean; onSeek: (ts: string) => void;
}) {
  const [filter, setFilter] = useState("all");
  const listRef = useRef<HTMLOListElement | null>(null);
  const now = currentTs ? ms(currentTs) : ms(startTs);
  const matched = useMemo(() => items.filter(FILTERS.find((f) => f.key === filter)!.match), [items, filter]);
  const shown = matched.filter((a) => ms(a.ts) <= now || isPlanned(a));
  const hiddenLater = matched.length - shown.length;
  const lastPastIdx = shown.reduce((acc, a, i) => (ms(a.ts) <= now ? i : acc), -1);

  useEffect(() => {
    if (!playing || lastPastIdx < 0) return;
    listRef.current?.children[lastPastIdx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [lastPastIdx, playing]);

  return (
    <section aria-labelledby="action-log-title" className="flex h-full min-h-0 flex-col gap-2">
      <div>
        <h2 id="action-log-title" className="text-[15px] font-semibold">What LEO did, and why</h2>
        <p className="text-[13px] text-[var(--leo-text-dim)]">Select an entry to move the replay to that moment.</p>
      </div>
      <div role="group" aria-label="Show" className="flex flex-wrap gap-1">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}
            className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === f.key
              ? "border-[var(--leo-text-dim)] bg-[var(--leo-panel-raised)] text-[var(--leo-text)]"
              : "border-[var(--leo-border)] text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"}`}>
            {f.label}
          </button>
        ))}
      </div>
      <ol ref={listRef} className="flex min-h-0 flex-col gap-1.5 overflow-y-auto pr-1">
        {shown.map((a, i) => {
          const t = ms(a.ts);
          const end = a.end ? ms(a.end) : t;
          const state = t > now ? "upcoming" : now <= end && a.end ? "now" : "done";
          const beforeDay = t < ms(startTs);
          const alert = a.severity === "bad" || a.severity === "warn";
          return (
            <li key={i} className={`rounded-md border ${state === "now"
              ? "border-[var(--leo-accent)] bg-[rgb(59_169_255/0.08)]"
              : state === "upcoming" ? "border-dashed border-[var(--leo-border)]" : "border-[var(--leo-border)] bg-[var(--leo-panel)]"}`}>
              <button type="button" onClick={() => onSeek(beforeDay ? startTs : a.ts)}
                aria-label={`${a.title}, ${fmtTime(a.ts, beforeDay)} IST. Move the replay here.`}
                className="w-full rounded-md px-2.5 pb-1 pt-2 text-left hover:bg-[var(--leo-panel-raised)]">
                <span className="flex items-center gap-2 text-xs text-[var(--leo-text-dim)]">
                  <span>
                    {fmtTime(a.ts, beforeDay)}{a.end && a.end !== a.ts ? `–${fmtTime(a.end)}` : ""}
                  </span>
                  <span className="text-[var(--leo-text)]">{actorLabel(a.actor)}</span>
                  {beforeDay && <span>day before</span>}
                  {state === "now" && <span className="ml-auto font-semibold text-[var(--leo-accent)]">now</span>}
                  {state === "upcoming" && <span className="ml-auto">planned</span>}
                </span>
                <span className="mt-0.5 flex items-baseline gap-1.5 text-sm">
                  {alert && <span aria-hidden className={a.severity === "bad" ? "font-bold text-[var(--leo-bad)]" : "font-bold text-[var(--leo-warn)]"}>!</span>}
                  <span className={a.severity === "bad" ? "text-[var(--leo-bad)]" : a.severity === "warn" ? "text-[var(--leo-warn)]" : ""}>{a.title}</span>
                </span>
                {a.detail && <span className="mt-0.5 block text-[13px] text-[var(--leo-text-dim)]">{a.detail}</span>}
                {a.reason && <span className="mt-0.5 block text-[13px]"><span className="text-[var(--leo-text-dim)]">Why: </span>{a.reason}</span>}
              </button>
              {a.link && (
                <Link href={a.link} className="mb-1.5 ml-2.5 inline-block text-[13px] text-[var(--leo-accent)] hover:underline">
                  Details
                </Link>
              )}
            </li>
          );
        })}
      </ol>
      {hiddenLater > 0 && (
        <p className="text-xs text-[var(--leo-text-dim)]">{hiddenLater} more entries appear as the replay reaches them.</p>
      )}
    </section>
  );
}
