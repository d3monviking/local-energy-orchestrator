"use client";

/**
 * The "what did LEO see coming" column: the day-ahead forecast as a
 * predicted-vs-actual transformer loading chart, then one card per event
 * the forecast predicted — when it was predicted, why, what LEO planned
 * to do about it, and (once the playhead reaches it) what actually
 * happened.
 */

import { ActionItem, EVENT_STYLE, Forecast, PredictedEvent, fmtTime, ms } from "./types";

type Props = {
  forecast: Forecast | null;
  events: PredictedEvent[];
  actions: ActionItem[];
  startTs: string;
  endTs: string;
  currentTs: string | null;
  leoEnabled: boolean;
  onSeek: (ts: string) => void;
};

function LoadingChart({ forecast, startTs, endTs, currentTs }: {
  forecast: Forecast; startTs: string; endTs: string; currentTs: string | null;
}) {
  const W = 300, H = 96, PAD = 4;
  const t0 = ms(startTs), t1 = ms(endTs);
  const pts = forecast.intervals.filter((i) => ms(i.ts_end) >= t0 && ms(i.ts_end) <= t1);
  const maxPct = Math.max(120, ...pts.map((p) => Math.max(p.trafo_pred_pct ?? 0, p.trafo_actual_pct ?? 0)));
  const x = (ts: string) => PAD + ((ms(ts) - t0) / (t1 - t0)) * (W - 2 * PAD);
  const y = (pct: number) => H - PAD - (pct / maxPct) * (H - 2 * PAD);
  const line = (key: "trafo_pred_pct" | "trafo_actual_pct") =>
    pts.filter((p) => p[key] != null).map((p) => `${x(p.ts_end).toFixed(1)},${y(p[key] as number).toFixed(1)}`).join(" ");
  const nowX = currentTs ? x(currentTs) : null;
  // Actual line only up to the playhead — the future hasn't happened yet.
  const actualPts = pts.filter((p) => p.trafo_actual_pct != null && (!currentTs || ms(p.ts_end) <= ms(currentTs)));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-24" role="img"
      aria-label="Transformer loading, forecast versus actual">
      <line x1={PAD} x2={W - PAD} y1={y(100)} y2={y(100)} stroke="#e0473e" strokeDasharray="3 3" strokeWidth={1} />
      <text x={W - PAD} y={y(100) - 2} textAnchor="end" fontSize="8" fill="#e0473e">100% rating</text>
      <polyline points={line("trafo_pred_pct")} fill="none" stroke="#9b7fe0" strokeWidth={1.5} strokeDasharray="4 2" />
      <polyline
        points={actualPts.map((p) => `${x(p.ts_end).toFixed(1)},${y(p.trafo_actual_pct as number).toFixed(1)}`).join(" ")}
        fill="none" stroke="#e6edf3" strokeWidth={1.5}
      />
      {nowX != null && <line x1={nowX} x2={nowX} y1={0} y2={H} stroke="#3ba9ff" strokeWidth={1} />}
    </svg>
  );
}

function relatedActions(ev: PredictedEvent, actions: ActionItem[]): ActionItem[] {
  const s = ms(ev.start) - 6 * 3600e3, e = ms(ev.end);
  return actions.filter((a) => {
    const t = ms(a.ts);
    const phase = a.meta?.phase as string | undefined;
    const phaseOk = ev.type === "transformer_overload" || ev.type === "load_shedding" || !phase || phase === ev.phase;
    if (a.kind.startsWith("battery_") && a.meta?.source === "plan") return phaseOk && t >= s && t <= e;
    if (a.kind.startsWith("battery_") && a.meta?.source === "live") return phaseOk && t >= ms(ev.start) && t <= e;
    if (a.kind.startsWith("battery_") && a.meta?.source === "pre_outage") return ev.type === "load_shedding";
    if (a.kind === "dr_offers") return ev.type !== "overvoltage" && ev.type !== "load_shedding";
    if (a.kind === "alert_sent" || (a.kind === "mode" && a.meta?.to === "pre_outage")) return ev.type === "load_shedding";
    if (a.kind === "recommendation") return phaseOk && ev.type !== "transformer_overload";
    return false;
  });
}

export default function ForecastPanel({ forecast, events, actions, startTs, endTs, currentTs, leoEnabled, onSeek }: Props) {
  const now = currentTs ? ms(currentTs) : ms(startTs);

  return (
    <div className="flex flex-col gap-3 min-h-0">
      <div>
        <h2 className="text-sm font-semibold">What LEO predicted</h2>
        {forecast?.issued_at ? (
          <p className="text-xs text-[var(--leo-text-dim)]">
            Day-ahead forecast issued {fmtTime(forecast.issued_at, true)} IST, the evening before.
            Based on tomorrow&apos;s weather forecast, calendar and past load.
          </p>
        ) : (
          <p className="text-xs text-[var(--leo-text-dim)]">No forecast saved for this run.</p>
        )}
      </div>

      {forecast && forecast.intervals.length > 0 && (
        <div className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] p-2">
          <p className="text-xs mb-1">Transformer loading ({forecast.transformer_kva ?? 100} kVA)</p>
          <LoadingChart forecast={forecast} startTs={startTs} endTs={endTs} currentTs={currentTs} />
          <div className="flex gap-3 text-[11px] text-[var(--leo-text-dim)]">
            <span><span className="inline-block w-3 border-t-2 border-dashed border-[#9b7fe0] align-middle mr-1" />forecast (P90)</span>
            <span><span className="inline-block w-3 border-t-2 border-[#e6edf3] align-middle mr-1" />actual so far</span>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2 overflow-y-auto min-h-0 pr-1">
        {events.length === 0 && (
          <p className="text-xs text-[var(--leo-text-dim)]">No events predicted for this day.</p>
        )}
        {events.map((ev, i) => {
          const style = EVENT_STYLE[ev.type] ?? { color: "#93a1b0", label: ev.type };
          const status = now < ms(ev.start) ? "upcoming" : now < ms(ev.end) ? "happening now" : "over";
          const revealed = now >= ms(ev.start);
          const response = leoEnabled ? relatedActions(ev, actions) : [];
          return (
            <button
              key={i}
              onClick={() => onSeek(ev.start)}
              className={`text-left rounded-md border p-2.5 bg-[var(--leo-panel)] hover:border-[var(--leo-accent)] ${
                status === "happening now" ? "border-2" : "border-[var(--leo-border)]"
              }`}
              style={status === "happening now" ? { borderColor: style.color } : undefined}
            >
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-semibold uppercase rounded px-1.5 py-0.5"
                  style={{ background: `${style.color}33`, color: style.color }}>
                  {style.label}
                </span>
                {ev.missed && (
                  <span className="text-[10px] rounded px-1.5 py-0.5 bg-[var(--leo-warn)]/20 text-[var(--leo-warn)]">missed by forecast</span>
                )}
                <span className={`text-[10px] ml-auto ${status === "happening now" ? "text-[var(--leo-text)] font-semibold" : "text-[var(--leo-text-dim)]"}`}>
                  {status}
                </span>
              </div>
              <p className="text-sm font-medium mt-1">{ev.title}</p>
              <p className="text-xs text-[var(--leo-text-dim)]">
                {ev.type === "unplanned_outage" || ev.missed ? "Happened" : "Predicted"} {fmtTime(ev.start)}–{fmtTime(ev.end)} IST
                {ev.predicted_at && ev.type !== "unplanned_outage" && <> · known since {fmtTime(ev.predicted_at, true)}</>}
              </p>

              {ev.why.length > 0 && (
                <ul className="mt-1.5 text-xs list-disc pl-4 text-[var(--leo-text-dim)]">
                  {ev.why.map((w, j) => <li key={j}>{w}</li>)}
                </ul>
              )}

              <div className="mt-1.5 text-xs">
                <span className="font-medium">LEO&apos;s response: </span>
                {!leoEnabled ? (
                  <span className="text-[var(--leo-text-dim)]">none — LEO is off on this run</span>
                ) : response.length === 0 ? (
                  <span className="text-[var(--leo-text-dim)]">
                    {ev.type === "unplanned_outage" ? "react on detection (see action log)" : "nothing scheduled"}
                  </span>
                ) : (
                  <ul className="list-disc pl-4 text-[var(--leo-text-dim)]">
                    {response.slice(0, 4).map((a, j) => <li key={j}>{a.title}</li>)}
                    {response.length > 4 && <li>+{response.length - 4} more in the action log</li>}
                  </ul>
                )}
              </div>

              <div className="mt-1.5 text-xs">
                <span className="font-medium">Outcome: </span>
                {!revealed ? (
                  <span className="text-[var(--leo-text-dim)]">not yet — play forward</span>
                ) : ev.actual ? (
                  <span>
                    happened {fmtTime(ev.actual.start)}–{fmtTime(ev.actual.end)} IST
                    {ev.actual.worst != null && <>, worst {ev.actual.worst}{ev.type === "transformer_overload" ? "%" : " V"}</>}
                  </span>
                ) : (
                  <span className="text-[var(--leo-ok)]">avoided — did not occur</span>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
