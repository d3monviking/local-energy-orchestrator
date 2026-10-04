"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import PhaseChip from "@/components/operator/PhaseChip";
import { fmtTime, ms, type Forecast, type PredictedEvent, type RunMeta } from "@/components/operator/types";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const PHASES = ["R", "Y", "B"] as const;

/** The two recorded days that have a day-ahead plan of their own. The
 *  load-shedding and outage runs reuse the peak day's plan. */
const DAYS = [
  { run: "normal", label: "Peak day" },
  { run: "surplus", label: "Sunny day" },
];

type PlanPhase = {
  phase: string;
  planner: string;
  reserve_kwh: number;
  approved_at: string | null;
  approved_by: string | null;
  intervals: { ts_end: string; setpoint_kw: number; mode: string }[];
};
type DrOffer = { household_id: string; level: number; is_holdout: boolean };
type DrEvent = { event_id: string; phase: string; window_start: string; window_end: string; target_kw: number; n_offers: number; n_holdout: number; offers: DrOffer[] };
type RunEvent = { ts: string; kind: string; payload: Record<string, unknown> };
type Decision = { decision: "approved" | "rejected" | "withdrawn"; by: string; reason: string | null; at: string };
type Status = "approved" | "rejected" | "pending";

const REJECT_REASONS = [
  "The forecast looks wrong for tomorrow",
  "A battery is down for maintenance",
  "The DISCOM has announced work on this transformer",
];

type Row = { ts: string; p10: number; p50: number; p90: number; setpoint: number; after: number };

const PLANNER_TEXT: Record<string, string> = {
  shave: "Peak shaving: charge from midday solar, discharge through the evening peak",
};

function istDate(ts: string, opts: Intl.DateTimeFormatOptions) {
  return new Date(ts).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", ...opts });
}

function kw(v: number, d = 1) {
  return `${v.toFixed(d)} kW`;
}

/** Peak draw, or peak export when reverse flow is the bigger problem. */
function worst(values: number[]) {
  let best = 0;
  for (const v of values) if (Math.abs(v) > Math.abs(best)) best = v;
  return best;
}

function describe(v: number) {
  return v < 0 ? `${kw(-v)} export` : `${kw(v)} draw`;
}

/* ------------------------------------------------------------------ chart */

function PhaseChart({ rows, rating, events }: { rows: Row[]; rating: number; events: PredictedEvent[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 960, PL = 52, PR = 12, TOP = 22, MAIN = 190, GAP = 18, STRIP = 64, AXIS = 22;
  const H = TOP + MAIN + GAP + STRIP + AXIS;
  if (rows.length === 0) return null;

  const t0 = ms(rows[0].ts) - 15 * 60_000;
  const t1 = ms(rows[rows.length - 1].ts);
  const x = (ts: number) => PL + ((ts - t0) / (t1 - t0)) * (W - PL - PR);

  const hi = Math.max(rating * 1.1, ...rows.map((r) => r.p90), ...rows.map((r) => r.after));
  const lo = Math.min(0, ...rows.map((r) => r.p10), ...rows.map((r) => r.after));
  const showNegRating = lo < -rating * 0.6;
  const yLo = showNegRating ? Math.min(lo, -rating * 1.1) : lo;
  const y = (v: number) => TOP + MAIN - ((v - yLo) / (hi - yLo)) * MAIN;

  const maxBatt = Math.max(1, ...rows.map((r) => Math.abs(r.setpoint)));
  const stripMid = TOP + MAIN + GAP + STRIP / 2;
  const by = (v: number) => stripMid - (v / maxBatt) * (STRIP / 2 - 2);

  const band = rows.map((r) => `${x(ms(r.ts))},${y(r.p90)}`).join(" ") + " " +
    [...rows].reverse().map((r) => `${x(ms(r.ts))},${y(r.p10)}`).join(" ");
  const line = (key: "p50" | "after") => rows.map((r) => `${x(ms(r.ts))},${y(r[key])}`).join(" ");

  // IST hour ticks every 3 h
  const ticks: number[] = [];
  const firstHour = Math.ceil(t0 / 3_600_000) * 3_600_000;
  for (let t = firstHour; t <= t1; t += 3_600_000) {
    const h = Number(new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", hour12: false }));
    if (h % 3 === 0) ticks.push(t);
  }
  const yTicks: number[] = [];
  const step = hi - yLo > 80 ? 20 : 10;
  for (let v = Math.ceil(yLo / step) * step; v <= hi; v += step) yTicks.push(v);

  const barW = Math.max(1, (W - PL - PR) / rows.length - 1);

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg) return;
    const box = svg.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    const ts = t0 + ((px - PL) / (W - PL - PR)) * (t1 - t0);
    let best = 0;
    for (let i = 0; i < rows.length; i++) if (Math.abs(ms(rows[i].ts) - ts) < Math.abs(ms(rows[best].ts) - ts)) best = i;
    setHover(px < PL || px > W - PR ? null : best);
  }

  const h = hover != null ? rows[hover] : null;

  return (
    <div>
      <p className="mb-1 min-h-[1.5rem] text-[13px] text-[var(--leo-text-dim)]" aria-live="off">
        {h ? (
          <>
            <span className="text-[var(--leo-text)]">{fmtTime(h.ts)} IST</span> · forecast {kw(h.p50)} · after plan{" "}
            <span className="text-[var(--leo-text)]">{kw(h.after)}</span> ·{" "}
            {h.setpoint > 0.05 ? `battery discharging ${kw(h.setpoint)}` : h.setpoint < -0.05 ? `battery charging ${kw(-h.setpoint)}` : "battery idle"}
          </>
        ) : (
          "Point at the chart to read any 15-minute interval."
        )}
      </p>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="block h-auto w-full select-none"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        aria-hidden
      >
        {/* predicted problems on this phase */}
        {events.map((ev, i) => {
          const a = Math.max(x(ms(ev.start)), PL), b = Math.min(x(ms(ev.end)), W - PR);
          if (b <= a) return null;
          return (
            <g key={i}>
              <rect x={a} y={TOP} width={b - a} height={MAIN} fill="rgb(224 71 62 / 0.10)" />
              <line x1={a} x2={a} y1={TOP} y2={TOP + MAIN} stroke="rgb(224 71 62 / 0.5)" strokeDasharray="2 3" />
            </g>
          );
        })}
        {events.length > 0 && (
          <text x={Math.max(x(Math.min(...events.map((e) => ms(e.start)))), PL) + 4} y={TOP - 8} fontSize={12} fill="#ff8a82">
            {[...new Set(events.map((ev) => (ev.type === "transformer_overload" ? "overload" : ev.type === "undervoltage" ? "low voltage" : ev.type === "overvoltage" ? "high voltage" : "problem")))]
              .join(" and ").replace(/^./, (c) => c.toUpperCase())} predicted
          </text>
        )}

        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PL} x2={W - PR} y1={y(v)} y2={y(v)} stroke="#26323f" strokeWidth={v === 0 ? 1.2 : 0.6} />
            <text x={PL - 8} y={y(v) + 4} fontSize={12} textAnchor="end" fill="#93a1b0">{v}</text>
          </g>
        ))}
        <text x={4} y={TOP + 4} fontSize={12} fill="#93a1b0">kW</text>

        <polygon points={band} fill="rgb(166 140 236 / 0.16)" />
        <polyline points={line("p50")} fill="none" stroke="#a68cec" strokeWidth={1.6} strokeDasharray="5 3" />
        <polyline points={line("after")} fill="none" stroke="#e6edf3" strokeWidth={2} />

        <line x1={PL} x2={W - PR} y1={y(rating)} y2={y(rating)} stroke="#e0473e" strokeWidth={1.2} strokeDasharray="6 4" />
        <text x={W - PR} y={y(rating) - 6} fontSize={12} textAnchor="end" fill="#ff8a82">phase rating {rating.toFixed(1)} kW</text>
        {showNegRating && (
          <>
            <line x1={PL} x2={W - PR} y1={y(-rating)} y2={y(-rating)} stroke="#e0473e" strokeWidth={1.2} strokeDasharray="6 4" />
            <text x={W - PR} y={y(-rating) + 16} fontSize={12} textAnchor="end" fill="#ff8a82">export limit</text>
          </>
        )}

        {/* battery strip: discharge up (solid), charge down (outlined) */}
        <line x1={PL} x2={W - PR} y1={stripMid} y2={stripMid} stroke="#26323f" />
        <text x={PL - 8} y={stripMid - STRIP / 2 + 12} fontSize={11} textAnchor="end" fill="#93a1b0">out</text>
        <text x={PL - 8} y={stripMid + STRIP / 2 - 2} fontSize={11} textAnchor="end" fill="#93a1b0">in</text>
        {rows.map((r, i) => {
          if (Math.abs(r.setpoint) < 0.05) return null;
          const xx = x(ms(r.ts)) - barW;
          const top = Math.min(by(r.setpoint), stripMid);
          const height = Math.abs(by(r.setpoint) - stripMid);
          return r.setpoint > 0 ? (
            <rect key={i} x={xx} y={top} width={barW} height={height} fill="#3fc6c6" />
          ) : (
            <rect key={i} x={xx + 0.5} y={top} width={Math.max(0.5, barW - 1)} height={height} fill="rgb(63 198 198 / 0.18)" stroke="#3fc6c6" strokeWidth={0.8} />
          );
        })}

        {ticks.map((t) => (
          <g key={t}>
            <text x={x(t)} y={H - 4} fontSize={12} textAnchor="middle" fill="#93a1b0">
              {new Date(t).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false })}
            </text>
          </g>
        ))}

        {h && <line x1={x(ms(h.ts))} x2={x(ms(h.ts))} y1={TOP} y2={TOP + MAIN + GAP + STRIP} stroke="#7cc4ff" strokeWidth={1} />}
      </svg>
    </div>
  );
}

function Legend() {
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-[var(--leo-text-dim)]">
      <li className="flex items-center gap-2"><svg width="22" height="8" aria-hidden><line x1="0" x2="22" y1="4" y2="4" stroke="#a68cec" strokeWidth="2" strokeDasharray="5 3" /></svg>Forecast load, most likely (band: likely range)</li>
      <li className="flex items-center gap-2"><svg width="22" height="8" aria-hidden><line x1="0" x2="22" y1="4" y2="4" stroke="#e6edf3" strokeWidth="2.5" /></svg>Load on the transformer after the plan</li>
      <li className="flex items-center gap-2"><span aria-hidden className="inline-block h-3 w-3 bg-[var(--leo-battery)]" />Battery discharging</li>
      <li className="flex items-center gap-2"><span aria-hidden className="inline-block h-3 w-3 border border-[var(--leo-battery)] bg-[rgb(63_198_198/0.18)]" />Battery charging</li>
      <li className="flex items-center gap-2"><svg width="22" height="8" aria-hidden><line x1="0" x2="22" y1="4" y2="4" stroke="#e0473e" strokeWidth="1.5" strokeDasharray="6 4" /></svg>Phase rating</li>
    </ul>
  );
}

/* ------------------------------------------------------------------- page */

export default function PlanReview() {
  const [run, setRun] = useState("normal");
  useEffect(() => {
    const r = new URLSearchParams(window.location.search).get("run");
    if (r && DAYS.some((d) => d.run === r)) setRun(r);
  }, []);
  const [meta, setMeta] = useState<RunMeta | null>(null);
  const [plan, setPlan] = useState<PlanPhase[] | null>(null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [predicted, setPredicted] = useState<PredictedEvent[]>([]);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);
  const [dr, setDr] = useState<DrEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const rejectRef = useRef<HTMLDialogElement>(null);
  const [status, setStatus] = useState<Status>("pending");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    setError(null);
    setPlan(null);
    try {
      const get = async (path: string) => {
        const r = await fetch(`${CLOUD_API_URL}${path}`);
        if (!r.ok) throw new Error(`${path} returned ${r.status}`);
        return r.json();
      };
      const [cat, p, f, pe, ev, d] = await Promise.all([
        get("/api/run_catalog"), get(`/api/plan/${run}`), get(`/api/forecast/${run}`),
        get(`/api/predicted_events/${run}`), get(`/api/events/${run}`), get(`/api/dr_events/${run}`),
      ]);
      setMeta((cat as RunMeta[]).find((m) => m.run_id === run) ?? null);
      setPlan(p.phases);
      setStatus(p.status ?? (p.phases[0]?.approved_at ? "approved" : "pending"));
      setDecisions(p.decisions ?? []);
      setForecast(f);
      setPredicted((pe as PredictedEvent[]).filter((e) => e.predicted_at));
      setRunEvents(ev);
      setDr(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run]);

  useEffect(() => {
    load();
  }, [load]);

  const rating = ((forecast?.transformer_kva ?? 100) / 3) * 0.95;

  const rowsByPhase = useMemo(() => {
    const out: Record<string, Row[]> = {};
    if (!plan || !forecast) return out;
    const fc = new Map(forecast.intervals.map((iv) => [ms(iv.ts_end), iv]));
    for (const p of plan) {
      out[p.phase] = p.intervals.flatMap((iv) => {
        const f = fc.get(ms(iv.ts_end))?.phases[p.phase];
        if (!f) return [];
        return [{ ts: iv.ts_end, p10: f.p10_kw, p50: f.p50_kw, p90: f.p90_kw, setpoint: iv.setpoint_kw, after: f.p50_kw - iv.setpoint_kw }];
      });
    }
    return out;
  }, [plan, forecast]);

  const summary = useMemo(() => {
    return PHASES.filter((ph) => rowsByPhase[ph]?.length).map((ph) => {
      const rows = rowsByPhase[ph];
      const before = worst(rows.map((r) => r.p50));
      const after = worst(rows.map((r) => r.after));
      const beforeAt = rows.find((r) => r.p50 === before)!.ts;
      const out = rows.reduce((s, r) => s + Math.max(0, r.setpoint) * 0.25, 0);
      const inn = rows.reduce((s, r) => s + Math.max(0, -r.setpoint) * 0.25, 0);
      const overBefore = rows.filter((r) => Math.abs(r.p50) > rating).length * 0.25;
      const overAfter = rows.filter((r) => Math.abs(r.after) > rating).length * 0.25;
      return { phase: ph, before, beforeAt, after, out, inn, overBefore, overAfter };
    });
  }, [rowsByPhase, rating]);

  const approval = plan?.[0] ? { at: plan[0].approved_at, by: plan[0].approved_by } : null;

  const pump = runEvents.find((e) => e.kind === "dr_auto_shift")?.payload as
    | { pump_kwh: number; pump_homes: string[]; pump_slot_ist: number } | undefined;
  const ac = runEvents.find((e) => e.kind === "dr_auto_ac");
  const acEnd = runEvents.find((e) => e.kind === "dr_event_end")?.ts;
  const acPayload = ac?.payload as { ac_kw: number; ac_homes: string[]; ac_payment_rs: number } | undefined;

  const offerCounts = (ev: DrEvent) => {
    const sent = ev.offers.filter((o) => !o.is_holdout);
    const paid = new Map<number, number>();
    for (const o of sent) paid.set(o.level, (paid.get(o.level) ?? 0) + 1);
    return { sent: sent.length, holdout: ev.offers.length - sent.length, paid };
  };

  const totalOut = summary.reduce((s, p) => s + p.out, 0);
  const overBeforeH = Math.max(0, ...summary.map((s) => s.overBefore));
  const sumBefore = summary.reduce((a, s) => a + s.overBefore, 0);
  const sumAfter = summary.reduce((a, s) => a + s.overAfter, 0);
  const fmtH = (h: number) => (h === 0 ? "no time" : `${h % 1 ? h.toFixed(2).replace(/0$/, "") : h} hours`);

  async function decide(action: "approve" | "reject" | "withdraw") {
    setBusy(true);
    setNotice("");
    try {
      const r = await fetch(`${CLOUD_API_URL}/api/plan/${run}/${action}`, {
        method: "POST",
        ...(action === "reject" ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) } : {}),
      });
      if (!r.ok) throw new Error(`the server returned ${r.status}`);
      const p = await fetch(`${CLOUD_API_URL}/api/plan/${run}`).then((x) => x.json());
      setPlan(p.phases);
      setStatus(p.status);
      setDecisions(p.decisions ?? []);
      setNotice(
        action === "approve" ? "Plan approved. The batteries will follow it and the DR messages go out now."
          : action === "reject" ? "Plan rejected. Nothing will be sent to the batteries or to households."
            : "Approval withdrawn. The plan is waiting for your decision again.",
      );
      dialogRef.current?.close();
      rejectRef.current?.close();
      setReason("");
    } catch (e) {
      setNotice(`Could not ${action} the plan: ${e instanceof Error ? e.message : e}. Try again.`);
    } finally {
      setBusy(false);
    }
  }

  // The recorded approval predates the decision log, so show it as the first entry.
  const history: Decision[] = decisions.length
    ? decisions
    : approval?.at ? [{ decision: "approved", by: approval.by ?? "operator", reason: null, at: approval.at }] : [];
  const lastRejected = [...decisions].reverse().find((d) => d.decision === "rejected");

  const dayTitle = meta ? istDate(meta.sim_start, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "";

  return (
    <main id="main-content" className="mx-auto w-full max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold leading-tight">Plan for {dayTitle || "…"}</h1>
          <p className="mt-1 text-sm text-[var(--leo-text-dim)]">
            {forecast?.issued_at ? <>Forecast made {fmtTime(forecast.issued_at, true)} IST. </> : null}
            Approve by 23:30 IST the evening before. The plan runs for 24 hours from 05:30 IST.
          </p>
        </div>
        <div role="group" aria-label="Recorded day" className="flex rounded-md border border-[var(--leo-border)] p-0.5">
          {DAYS.map((d) => (
            <button
              key={d.run}
              type="button"
              aria-pressed={run === d.run}
              onClick={() => setRun(d.run)}
              className={`rounded px-3 py-1.5 text-sm ${run === d.run ? "bg-[var(--leo-panel-raised)] text-[var(--leo-text)]" : "text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"}`}
            >
              {d.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div role="alert" className="mt-6 rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 text-sm">
          <p className="font-medium text-[var(--leo-bad)]">The plan could not be loaded.</p>
          <p className="mt-1 text-[var(--leo-text-dim)]">{error}. Check that the cloud API is running on {CLOUD_API_URL}.</p>
          <button type="button" onClick={load} className="mt-3 rounded-md border border-[var(--leo-border)] px-3 py-1.5 hover:bg-[var(--leo-panel-raised)]">Try again</button>
        </div>
      )}
      {!error && !plan && <p className="mt-8 text-sm text-[var(--leo-text-dim)]">Loading the plan…</p>}
      {!error && plan && plan.length === 0 && (
        <p className="mt-8 text-sm text-[var(--leo-text-dim)]">No plan has been made for this day yet. Plans are drafted at 19:30 IST, after the day-ahead forecast.</p>
      )}

      {plan && plan.length > 0 && (
        <>
          {/* decision workflow */}
          <ol aria-label="Planning steps" className="mt-6 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            {[
              { n: 1, title: "Forecast made", when: forecast?.issued_at ? `${fmtTime(forecast.issued_at, true)} IST` : "19:30 IST", state: "done" },
              { n: 2, title: "Plan drafted by LEO", when: "after the forecast", state: "done" },
              { n: 3, title: "Your decision", when: "by 23:30 IST", state: status === "pending" ? "current" : status === "rejected" ? "stopped" : "done" },
              { n: 4, title: "Plan runs", when: "05:30 IST, for 24 hours", state: status === "approved" ? "next" : "blocked" },
            ].map((st) => (
              <li key={st.n} aria-current={st.state === "current" ? "step" : undefined}
                className={`flex items-start gap-2.5 rounded-md border px-3 py-2 ${st.state === "current" ? "border-[var(--leo-warn)] bg-[rgb(224_167_46/0.08)]" : "border-[var(--leo-border)]"}`}>
                <span aria-hidden className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                  st.state === "done" ? "bg-[var(--leo-ok)] text-[#06140c]"
                    : st.state === "current" ? "bg-[var(--leo-warn)] text-[#1a1204]"
                      : st.state === "stopped" ? "bg-[var(--leo-bad-fill)] text-white"
                        : "border border-[var(--leo-border)] text-[var(--leo-text-dim)]"}`}>
                  {st.state === "done" ? "✓" : st.state === "stopped" ? "✕" : st.n}
                </span>
                <span>
                  <span className="block font-medium">{st.title}</span>
                  <span className="block text-[13px] text-[var(--leo-text-dim)]">
                    {st.state === "stopped" ? "rejected" : st.state === "blocked" ? "only after approval" : st.when}
                  </span>
                </span>
              </li>
            ))}
          </ol>

          <section
            aria-labelledby="decision"
            className={`mt-3 rounded-lg border p-4 ${status === "pending" ? "border-[var(--leo-warn)] bg-[rgb(224_167_46/0.08)]" : status === "rejected" ? "border-[var(--leo-bad-fill)] bg-[rgb(224_71_62/0.08)]" : "border-[var(--leo-border)] bg-[var(--leo-panel)]"}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="max-w-[70ch]">
                <h2 id="decision" className="text-base font-semibold">
                  {status === "approved" ? "Approved" : status === "rejected" ? "Rejected" : "Waiting for your decision"}
                </h2>
                <p className="mt-0.5 text-sm text-[var(--leo-text-dim)]">
                  {status === "approved"
                    ? `Approved by ${approval?.by} at ${fmtTime(approval!.at!, true)} IST. The batteries follow this plan, and the DR messages below go out at approval.`
                    : status === "rejected"
                      ? `Rejected by ${lastRejected?.by}: “${lastRejected?.reason}”. The batteries get no schedule from this plan and no messages go out. You can still approve it before 05:30 IST.`
                      : "Review what the plan does below, then approve or reject it. Nothing reaches the batteries or any household until you approve."}
                </p>
              </div>
              <div className="flex items-center gap-3">
                {status === "approved" ? (
                  <button type="button" disabled={busy} onClick={() => decide("withdraw")} className="rounded-md border border-[var(--leo-border)] px-3 py-2 text-sm hover:bg-[var(--leo-panel-raised)] disabled:opacity-60">
                    {busy ? "Withdrawing…" : "Withdraw approval"}
                  </button>
                ) : (
                  <>
                    {status === "pending" && (
                      <button type="button" disabled={busy} onClick={() => rejectRef.current?.showModal()} className="rounded-md border border-[var(--leo-bad-fill)] px-4 py-2 text-sm font-semibold text-[var(--leo-bad)] hover:bg-[rgb(224_71_62/0.12)] disabled:opacity-60">
                        Reject…
                      </button>
                    )}
                    <button type="button" disabled={busy} onClick={() => dialogRef.current?.showModal()} className="rounded-md bg-[var(--leo-accent)] px-4 py-2 text-sm font-semibold text-[#06121f] hover:brightness-110 disabled:opacity-60">
                      {status === "rejected" ? "Approve instead…" : "Approve…"}
                    </button>
                  </>
                )}
              </div>
            </div>
            <p role="status" className="mt-3 text-sm empty:hidden">{notice}</p>
            {status !== "approved" && (
              <p className="mt-3 text-[13px] text-[var(--leo-text-dim)]">
                This is a recorded day, so the map replay still shows what happened under the approved plan.
              </p>
            )}
            {history.length > 0 && (
              <details className="mt-3 text-sm">
                <summary className="cursor-pointer text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]">Decision history ({history.length})</summary>
                <ol className="mt-2 space-y-1 border-l border-[var(--leo-border)] pl-3">
                  {history.map((d, i) => (
                    <li key={i}>
                      <span className="text-[var(--leo-text-dim)]">{fmtTime(d.at, true)} IST</span>{" "}
                      <span className="font-medium">{d.decision === "approved" ? "Approved" : d.decision === "rejected" ? "Rejected" : "Approval withdrawn"}</span> by {d.by}
                      {d.reason && <>: “{d.reason}”</>}
                    </li>
                  ))}
                </ol>
              </details>
            )}
          </section>

          {/* expected outcome */}
          <section aria-labelledby="outcome" className="mt-8">
            <h2 id="outcome" className="text-lg font-semibold">What the plan should do</h2>
            <p className="mt-1 max-w-[70ch] text-sm text-[var(--leo-text-dim)]">
              {overBeforeH > 0
                ? `On the most likely forecast the three phases spend ${fmtH(sumBefore)} above their rating in total. With the batteries that falls to ${fmtH(sumAfter)}, before counting demand response.`
                : "On the most likely forecast no phase goes above its rating. The batteries still move solar into the evening."}{" "}
              Batteries deliver {totalOut.toFixed(0)} kWh in total. {PLANNER_TEXT[plan[0].planner] ?? plan[0].planner}.
            </p>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-[13px] text-[var(--leo-text-dim)]">
                  <tr className="border-b border-[var(--leo-border)]">
                    <th className="py-2 pr-4 font-normal">Phase</th>
                    <th className="py-2 pr-4 font-normal">Forecast peak</th>
                    <th className="py-2 pr-4 font-normal">After plan</th>
                    <th className="py-2 pr-4 text-right font-normal">Hours over rating</th>
                    <th className="py-2 pr-4 text-right font-normal">Battery in / out</th>
                    <th className="py-2 text-right font-normal">Kept for outages</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.map((s) => {
                    const pBefore = (Math.abs(s.before) / rating) * 100;
                    const pAfter = (Math.abs(s.after) / rating) * 100;
                    return (
                      <tr key={s.phase} className="border-b border-[var(--leo-border)]">
                        <td className="py-2.5 pr-4"><PhaseChip phase={s.phase} /></td>
                        <td className="py-2.5 pr-4">
                          {describe(s.before)} at {fmtTime(s.beforeAt)}{" "}
                          <span className={pBefore > 100 ? "text-[var(--leo-bad)]" : "text-[var(--leo-text-dim)]"}>({pBefore.toFixed(0)}% of rating)</span>
                        </td>
                        <td className="py-2.5 pr-4">
                          {describe(s.after)}{" "}
                          <span className={pAfter > 100 ? "text-[var(--leo-bad)]" : "text-[var(--leo-ok)]"}>({pAfter.toFixed(0)}%{pAfter > 100 ? ", still over" : ""})</span>
                        </td>
                        <td className="py-2.5 pr-4 text-right">{s.overBefore} h → {s.overAfter} h</td>
                        <td className="py-2.5 pr-4 text-right">{s.inn.toFixed(0)} / {s.out.toFixed(0)} kWh</td>
                        <td className="py-2.5 text-right">{plan.find((p) => p.phase === s.phase)?.reserve_kwh.toFixed(0)} kWh</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {/* demand response */}
          {(pump || acPayload || dr.length > 0) && (
            <section aria-labelledby="dr" className="mt-10">
              <h2 id="dr" className="text-lg font-semibold">Demand response in this plan</h2>
              <ul className="mt-3 divide-y divide-[var(--leo-border)] border-y border-[var(--leo-border)] text-sm">
                {pump && (
                  <li className="grid gap-1 py-3 md:grid-cols-[220px_1fr]">
                    <span className="font-medium">Pumps moved to midday</span>
                    <span className="text-[var(--leo-text-dim)]">
                      {pump.pump_homes.length} irrigation pumps on smart relays run for an hour from {String(Math.floor(pump.pump_slot_ist)).padStart(2, "0")}:{String(Math.round((pump.pump_slot_ist % 1) * 60)).padStart(2, "0")} IST, on solar, instead of 18:00–19:00. {pump.pump_kwh.toFixed(1)} kWh moved off the evening peak.
                    </span>
                  </li>
                )}
                {acPayload && ac && (
                  <li className="grid gap-1 py-3 md:grid-cols-[220px_1fr]">
                    <span className="font-medium">Air conditioners eased</span>
                    <span className="text-[var(--leo-text-dim)]">
                      {acPayload.ac_homes.length} enrolled ACs are pre-cooled, then cycled down from {fmtTime(ac.ts)}{acEnd ? `–${fmtTime(acEnd)}` : ""} IST, about {kw(acPayload.ac_kw)} less load. Each home is paid ₹{acPayload.ac_payment_rs} for the event.
                    </span>
                  </li>
                )}
                {dr.map((ev) => {
                  const c = offerCounts(ev);
                  const amounts = [...c.paid.entries()].sort((a, b) => a[0] - b[0])
                    .map(([lvl, n]) => (lvl === 0 ? `${n} asked without payment` : `${n} offered ₹${lvl}`)).join(", ");
                  return (
                    <li key={ev.event_id} className="grid gap-1 py-3 md:grid-cols-[220px_1fr]">
                      <span className="font-medium">SMS offers, <PhaseChip phase={ev.phase} /></span>
                      <span className="text-[var(--leo-text-dim)]">
                        {fmtTime(ev.window_start)}–{fmtTime(ev.window_end)} IST, aiming for {kw(ev.target_kw)} less load. {c.sent} households get a message ({amounts}).{" "}
                        {c.holdout} are held back on purpose so the saving can be measured against them.
                      </span>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {/* predicted problems */}
          {predicted.length > 0 && (
            <section aria-labelledby="predicted" className="mt-10">
              <h2 id="predicted" className="text-lg font-semibold">What the forecast warns about</h2>
              <ul className="mt-3 space-y-3 text-sm">
                {predicted.map((ev, i) => (
                  <li key={i} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] px-4 py-3">
                    <p className="font-medium">{ev.title}</p>
                    <p className="text-[var(--leo-text-dim)]">
                      {fmtTime(ev.start)}–{fmtTime(ev.end)} IST, worst at {fmtTime(ev.worst_ts)}. {ev.why.filter(Boolean).join(". ")}.
                    </p>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* per-phase detail */}
          <section aria-labelledby="phases" className="mt-10">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <h2 id="phases" className="text-lg font-semibold">Phase by phase</h2>
              <Legend />
            </div>
            {PHASES.filter((ph) => rowsByPhase[ph]?.length).map((ph) => {
              const s = summary.find((x) => x.phase === ph)!;
              return (
                <div key={ph} className="mt-6 border-t border-[var(--leo-border)] pt-4">
                  <h3 className="flex flex-wrap items-baseline gap-x-3 text-base">
                    <PhaseChip phase={ph} />
                    <span className="text-sm font-normal text-[var(--leo-text-dim)]">
                      forecast peak {describe(s.before)}, after plan {describe(s.after)}. Charges {s.inn.toFixed(0)} kWh, discharges {s.out.toFixed(0)} kWh.
                    </span>
                  </h3>
                  <div className="mt-2">
                    <PhaseChart rows={rowsByPhase[ph]} rating={rating} events={predicted.filter((e) => e.phase === ph)} />
                  </div>
                </div>
              );
            })}
          </section>
        </>
      )}

      <dialog
        ref={dialogRef}
        aria-labelledby="confirm-title"
        className="m-auto w-[min(560px,calc(100vw-32px))] rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-0 text-[var(--leo-text)] backdrop:bg-black/60"
      >
        <form method="dialog" className="p-5" onSubmit={(e) => { e.preventDefault(); decide("approve"); }}>
          <h2 id="confirm-title" className="text-lg font-semibold">Approve the plan for {dayTitle}?</h2>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-[var(--leo-text-dim)]">
            {summary.map((s) => (
              <li key={s.phase}>
                Phase {s.phase}: forecast peak {describe(s.before)} brought to {describe(s.after)}, {s.out.toFixed(0)} kWh from the battery.
              </li>
            ))}
            {pump && <li>{pump.pump_homes.length} pumps moved to midday.</li>}
            {acPayload && <li>{acPayload.ac_homes.length} ACs eased in the evening, ₹{acPayload.ac_payment_rs} each.</li>}
            {dr.map((ev) => <li key={ev.event_id}>{offerCounts(ev).sent} SMS offers sent now for phase {ev.phase}.</li>)}
          </ul>
          <p className="mt-3 text-sm">You can withdraw approval until the plan starts at 05:30 IST.</p>
          <div className="mt-5 flex justify-end gap-3">
            <button type="button" onClick={() => dialogRef.current?.close()} className="rounded-md border border-[var(--leo-border)] px-3 py-2 text-sm hover:bg-[var(--leo-panel-raised)]">Cancel</button>
            <button type="submit" disabled={busy} className="rounded-md bg-[var(--leo-accent)] px-4 py-2 text-sm font-semibold text-[#06121f] hover:brightness-110 disabled:opacity-60">
              {busy ? "Approving…" : "Approve plan"}
            </button>
          </div>
        </form>
      </dialog>

      <dialog
        ref={rejectRef}
        aria-labelledby="reject-title"
        className="m-auto w-[min(560px,calc(100vw-32px))] rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-0 text-[var(--leo-text)] backdrop:bg-black/60"
      >
        <form method="dialog" className="p-5" onSubmit={(e) => { e.preventDefault(); if (reason.trim()) decide("reject"); }}>
          <h2 id="reject-title" className="text-lg font-semibold">Reject the plan for {dayTitle}?</h2>
          <p className="mt-2 text-sm text-[var(--leo-text-dim)]">
            The batteries get no schedule from this plan and no DR messages go out. Your reason is kept with the decision.
          </p>
          <label htmlFor="reject-reason" className="mt-4 block text-sm font-medium">Reason</label>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {REJECT_REASONS.map((r) => (
              <button key={r} type="button" onClick={() => setReason(r)}
                className="rounded-full border border-[var(--leo-border)] px-2.5 py-1 text-xs text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]">
                {r}
              </button>
            ))}
          </div>
          <textarea id="reject-reason" required value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
            placeholder="Why should this plan not run?"
            className="mt-2 w-full rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-2 text-sm" />
          <div className="mt-5 flex justify-end gap-3">
            <button type="button" onClick={() => rejectRef.current?.close()} className="rounded-md border border-[var(--leo-border)] px-3 py-2 text-sm hover:bg-[var(--leo-panel-raised)]">Cancel</button>
            <button type="submit" disabled={busy || !reason.trim()} className="rounded-md bg-[#c4352d] px-4 py-2 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-50">
              {busy ? "Rejecting…" : "Reject plan"}
            </button>
          </div>
        </form>
      </dialog>
    </main>
  );
}
