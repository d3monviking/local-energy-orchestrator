"use client";

/**
 * Impact & economics: the two things the brief scores that a single demo
 * day can't show — a measured reliability improvement against a defined
 * baseline, and unit economics for each party. Everything here comes from
 * /api/impact (eval/sweep.py over one week per month, priced by
 * eval/economics.py from economics.yaml).
 */

import { useEffect, useMemo, useState } from "react";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

type Dict = Record<string, number>;
type Result = {
  config: string;
  label: string;
  physical: Dict & { battery_life_years: number | null; dr_offered_rs: Record<string, number> };
  operator: {
    capex: Dict; capex_total: number; revenue: Dict; revenue_total: number; payouts: number; opex: Dict;
    opex_total: number; net_cash_per_year: number; npv: number; payback_years: number | null; alpha: number;
    break_even_rate_rs_per_kwh: number | null; max_battery_price_at_discom_max_rate?: number | null;
    grant_pct_needed_at_discom_max_rate?: number | null; npv_at_discom_max_rate?: number;
  };
  discom: Dict & { net: number; max_rate_rs_per_kwh: number | null; npv: number; gross_saving: number };
  households: Dict & { per_household?: Record<string, number> };
  reliability: Record<string, number | null>;
  system_npv: number;
  system_thresholds: Record<string, number | null>;
  sensitivity: { case: string; operator_npv: number; discom_net: number; payback_years: number | null }[];
};
type Monthly = {
  config: string; month: string; evening_peak_kw: number; peak_loading_pct: number; aging_hours: number;
  discharge_kwh: number; dr_kwh: number; undervoltage_cust_h: number; crit_out_h: number; crit_served_h: number;
};
type Impact = {
  sweep_id: string; computed_at: string; scenario_name?: string; scale_to_year: number; weeks: string[][]; recommended: string;
  recommended_storage?: string;
  viable: string[]; results: Result[]; monthly: Monthly[]; assumptions: Record<string, Dict>;
  assumptions_yaml: string; backup_premises: { household_id: string; critical_class: string; phase: string }[];
};

const rs = (x: number | null | undefined, digits = 0) => {
  if (x == null || Number.isNaN(x)) return "—";
  const sign = x < 0 ? "−" : "";
  const a = Math.abs(x);
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
  return `${sign}₹${a.toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
};
const num = (x: number | null | undefined, digits = 0) =>
  x == null || Number.isNaN(x) ? "—" : x.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
const pctChange = (from: number, to: number) => (from ? ((to - from) / from) * 100 : 0);

const LABELS: Record<string, string> = {
  dfpo_capacity: "DFPO capacity (₹2,000/kW-yr)", evening_energy: "Evening-energy payment",
  paid_to_operator_capacity: "Paid to operator: capacity", paid_to_operator_energy: "Paid to operator: evening energy",
  smart_relays_controllers: "Smart pump relays + AC controllers", energy_settlement: "Energy settlement (ToD)", backup_fees: "Backup fees",
  battery_packs: "Second-life battery packs", inverters: "Hybrid inverters", backup_circuit: "Backup circuit + meters",
  sensors_and_gateways: "Sensors, CTs, gateways", installation: "Installation", registration_setup: "Aggregator registration (share)",
  technician: "Technician (share)", cloud_connectivity: "Cloud + data", sms: "SMS / IVR", insurance: "Insurance",
  maintenance: "Maintenance", mv_agency: "Independent M&V (share)",
  power_purchase_saved: "Peak power purchase avoided", dfpo_penalty_avoided: "DFPO shortfall penalty avoided",
  dt_failures_avoided: "Transformer failures avoided", paid_to_operator_dfpo: "Paid to operator (DFPO)",
  energy_settlement_with_operator: "Energy settlement with operator", retail_revenue_lost_to_dr: "Retail sales lost to DR",
};

function Kpi({ title, base, leo, unit, better, note }: {
  title: string; base: string; leo: string; unit?: string; better: string | null; note: string;
}) {
  return (
    <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 flex flex-col gap-1">
      <p className="text-[13px] font-medium text-[var(--leo-text-dim)]">{title}</p>
      <p className="text-2xl font-semibold">
        <span className="text-[var(--leo-text-dim)] text-lg">{base}</span>
        <span className="mx-2 text-[var(--leo-text-dim)]">→</span>
        {leo}
        {unit && <span className="text-sm font-normal text-[var(--leo-text-dim)]"> {unit}</span>}
      </p>
      {better && <p className="text-xs font-medium text-[var(--leo-ok)]">{better}</p>}
      <p className="text-xs text-[var(--leo-text-dim)] leading-relaxed">{note}</p>
    </div>
  );
}

function MonthlyChart({ rows, config, field, label, unit, refLine, refLabel }: {
  rows: Monthly[]; config: string; field: keyof Monthly; label: string; unit: string; refLine?: number; refLabel?: string;
}) {
  const months = [...new Set(rows.map((r) => r.month))].sort();
  const get = (c: string, m: string) => Number(rows.find((r) => r.config === c && r.month === m)?.[field] ?? 0);
  const W = 520, H = 170, L = 40, R = 8, T = 10, B = 24;
  const max = Math.max(refLine ?? 0, ...months.flatMap((m) => [get("baseline", m), get(config, m)])) * 1.08 || 1;
  const x = (i: number) => L + (i + 0.5) * ((W - L - R) / months.length);
  const y = (v: number) => T + (1 - v / max) * (H - T - B);
  const bw = ((W - L - R) / months.length) * 0.32;
  return (
    <figure className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-3">
      <figcaption className="text-sm font-medium mb-1">{label}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`${label} by month, without and with LEO`}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={L} x2={W - R} y1={y(max * f)} y2={y(max * f)} stroke="var(--leo-border)" strokeWidth={0.5} />
            <text x={L - 4} y={y(max * f) + 3} fontSize="11" textAnchor="end" fill="var(--leo-text-dim)">{num(max * f)}</text>
          </g>
        ))}
        {refLine != null && (
          <g>
            <line x1={L} x2={W - R} y1={y(refLine)} y2={y(refLine)} stroke="var(--leo-bad)" strokeDasharray="4 3" strokeWidth={1} />
            <text x={W - R} y={y(refLine) - 3} fontSize="11" textAnchor="end" fill="var(--leo-bad)">{refLabel}</text>
          </g>
        )}
        {months.map((m, i) => (
          <g key={m}>
            <rect x={x(i) - bw - 1} y={y(get("baseline", m))} width={bw} height={H - B - y(get("baseline", m))} fill="#5b6673">
              <title>{`No LEO, ${m.slice(0, 7)}: ${num(get("baseline", m), 1)} ${unit}`}</title>
            </rect>
            <rect x={x(i) + 1} y={y(get(config, m))} width={bw} height={H - B - y(get(config, m))} fill="var(--leo-accent)">
              <title>{`With LEO, ${m.slice(0, 7)}: ${num(get(config, m), 1)} ${unit}`}</title>
            </rect>
            <text x={x(i)} y={H - 8} fontSize="11" textAnchor="middle" fill="var(--leo-text-dim)">
              {new Date(m).toLocaleString("en-IN", { month: "short" })}
            </text>
          </g>
        ))}
      </svg>
      <div className="flex gap-4 text-xs text-[var(--leo-text-dim)]">
        <span><span className="inline-block w-2.5 h-2.5 mr-1 align-middle bg-[#5b6673]" />No LEO</span>
        <span><span className="inline-block w-2.5 h-2.5 mr-1 align-middle bg-[var(--leo-accent)]" />With LEO</span>
        <span className="ml-auto">{unit}</span>
      </div>
    </figure>
  );
}

function Ledger({ title, lines, total, totalLabel, tone }: {
  title: string; lines: [string, number][]; total: number; totalLabel: string; tone?: "ok" | "bad";
}) {
  return (
    <div>
      <p className="text-[13px] font-medium text-[var(--leo-text-dim)] mb-1">{title}</p>
      <table className="w-full text-sm">
        <tbody>
          {lines.map(([k, v]) => (
            <tr key={k} className="border-b border-[var(--leo-border)]/50">
              <td className="py-1 text-[var(--leo-text-dim)]">{LABELS[k] ?? k}</td>
              <td className={`py-1 text-right tabular-nums ${v < 0 ? "text-[var(--leo-bad)]" : ""}`}>{rs(v)}</td>
            </tr>
          ))}
          <tr>
            <td className="py-1.5 font-medium">{totalLabel}</td>
            <td className={`py-1.5 text-right font-semibold tabular-nums ${
              tone === "ok" ? "text-[var(--leo-ok)]" : tone === "bad" ? "text-[var(--leo-bad)]" : ""}`}>{rs(total)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

const THRESHOLD_TEXT: Record<string, [string, string, (A: Impact["assumptions"]) => number | undefined]> = {
  dfpo_penalty_rs_per_kw_year: ["DFPO shortfall penalty, ₹/kW-yr", "≥", (A) => A.dfpo?.penalty_rs_per_kw_year],
  peak_purchase_rs_per_kwh: ["DISCOM's evening power price", "≥", (A) => A.discom?.peak_purchase_rs_per_kwh],
  battery_rs_per_kwh: ["Second-life battery packs", "≤", (A) => A.capex?.battery_rs_per_kwh],
  inverter_rs_per_kw: ["Hybrid inverters", "≤", (A) => A.capex?.inverter_rs_per_kw],
};

function DealZone({ r, data }: { r: Result; data: Impact }) {
  const lo = r.operator.break_even_rate_rs_per_kwh;
  const hi = r.discom.max_rate_rs_per_kwh;
  if (lo == null || hi == null) return null;
  const scale = Math.max(lo, hi) * 1.25;
  const pos = (v: number) => `${(v / scale) * 100}%`;
  const overlap = hi >= lo;
  const thresholds = Object.entries(r.system_thresholds ?? {}).filter(([k, v]) => v != null && THRESHOLD_TEXT[k]);
  return (
    <section className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
      <h3 className="text-sm font-semibold">Is there a deal? The contract both sides can live with</h3>
      <p className="text-xs text-[var(--leo-text-dim)] mb-4">
        Two-part contract. Capacity: DFPO is measured in kW at the peak instant, paid at the MERC benchmark of
        ₹2,000/kW-yr ({num(r.physical.verified_peak_kw, 1)} kW verified here). Energy: a payment per verified kWh of evening
        reduction ({num(r.physical.flex_kwh)} kWh/yr from the battery and demand response) — most of the DISCOM&apos;s gain is
        evening power it no longer buys at ₹10/kWh, so it can share it. The operator needs at least its break-even energy
        rate to recover its capital over 10 years; the DISCOM should pay up to the rate where its own benefit hits zero.
        Payments between them cancel out, so a deal exists exactly when their combined value is positive
        (now {rs(r.system_npv)} over 10 years).
      </p>
      <div className="relative h-10 rounded bg-[var(--leo-panel-raised)]">
        {overlap ? (
          <div className="absolute inset-y-0 bg-[var(--leo-ok)]/30" style={{ left: pos(lo), width: `calc(${pos(hi)} - ${pos(lo)})` }} />
        ) : (
          <div className="absolute inset-y-0 bg-[var(--leo-bad)]/25" style={{ left: pos(hi), width: `calc(${pos(lo)} - ${pos(hi)})` }} />
        )}
        <div className="absolute inset-y-0 w-0.5 bg-[var(--leo-warn)]" style={{ left: pos(lo) }} />
        <div className="absolute inset-y-0 w-0.5 bg-[var(--leo-accent)]" style={{ left: pos(hi) }} />
      </div>
      <div className="relative h-10 text-xs">
        <span className={`absolute text-[var(--leo-warn)] ${lo <= hi ? "-translate-x-full text-right pr-1" : "pl-1"}`} style={{ left: pos(lo) }}>
          operator needs<br />≥ ₹{num(lo, 2)}/kWh
        </span>
        <span className={`absolute text-[var(--leo-accent)] ${lo <= hi ? "pl-1" : "-translate-x-full text-right pr-1"}`} style={{ left: pos(hi) }}>
          DISCOM can pay<br />≤ ₹{num(hi, 2)}/kWh
        </span>
      </div>
      {overlap ? (
        <p className="text-sm mt-2 text-[var(--leo-ok)]">
          A deal exists: ₹2,000/kW-yr capacity plus any evening-energy rate between ₹{num(lo, 2)} and ₹{num(hi, 2)}/kWh leaves
          both sides better off (contract assumed here: ₹{num(data.assumptions.dfpo?.evening_energy_rs_per_kwh, 2)}/kWh).
        </p>
      ) : (
        <div className="mt-2 text-sm">
          <p>
            No rate works at these prices: the gap is ₹{num(lo - hi, 2)}/kWh. It closes if{" "}
            <b>any one</b> of these turns out true:
          </p>
          <table className="text-xs mt-2">
            <tbody>
              {thresholds.map(([k, v]) => {
                const [label, op, cur] = THRESHOLD_TEXT[k];
                return (
                  <tr key={k}>
                    <td className="pr-4 py-0.5 text-[var(--leo-text-dim)]">{label}</td>
                    <td className="pr-4 py-0.5 font-medium">{op} ₹{num(v, (v as number) < 100 ? 1 : 0)}</td>
                    <td className="py-0.5 text-[var(--leo-text-dim)]">assumed ₹{num(cur(data.assumptions), (cur(data.assumptions) ?? 0) < 100 ? 1 : 0)}</td>
                  </tr>
                );
              })}
              {r.operator.grant_pct_needed_at_discom_max_rate != null && (
                <tr>
                  <td className="pr-4 py-0.5 text-[var(--leo-text-dim)]">Capital grant (VGF / CSR / RDSS)</td>
                  <td className="pr-4 py-0.5 font-medium">≥ {num(r.operator.grant_pct_needed_at_discom_max_rate)}%</td>
                  <td className="py-0.5 text-[var(--leo-text-dim)]">assumed {num(data.assumptions.finance?.capital_grant_pct)}%</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Assumptions({ yaml }: { yaml: string }) {
  const rows: { section: string; key: string; value: string; note: string }[] = [];
  let section = "";
  for (const line of yaml.split("\n")) {
    const sec = line.match(/^([a-z_]+):\s*$/);
    if (sec) { section = sec[1]; continue; }
    const kv = line.match(/^\s{2}([a-z_]+):\s*([^#]+?)\s*(?:#\s*(.*))?$/);
    if (kv) rows.push({ section, key: kv[1], value: kv[2], note: kv[3] ?? "" });
  }
  return (
    <details className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
      <summary className="text-sm font-semibold cursor-pointer">
        Assumptions ({rows.filter((r) => r.note.startsWith("verify")).length} still to be sourced)
      </summary>
      <table className="w-full text-xs mt-3">
        <thead className="text-[var(--leo-text-dim)] text-left">
          <tr><th className="py-1">Section</th><th>Assumption</th><th className="text-right pr-3">Value</th><th>Source / status</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.section + r.key} className="border-t border-[var(--leo-border)]/50 align-top">
              <td className="py-1 text-[var(--leo-text-dim)]">{r.section}</td>
              <td className="py-1">{r.key.replaceAll("_", " ")}</td>
              <td className="py-1 text-right pr-3 tabular-nums">{r.value}</td>
              <td className={`py-1 ${r.note.startsWith("verify") ? "text-[var(--leo-warn)]" : "text-[var(--leo-text-dim)]"}`}>{r.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export default function ImpactReport() {
  const [data, setData] = useState<Impact | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<string | null>(null);
  const [sweep, setSweep] = useState<"year" | "urban">("year");

  useEffect(() => {
    fetch(`${CLOUD_API_URL}/api/impact?sweep_id=${sweep}`)
      .then(async (r) => (r.ok ? r.json() : Promise.reject(await r.text())))
      .then((d: Impact) => {
        setData(d);
        setConfig((c) => (c && d.results.some((x) => x.config === c) ? c : d.recommended_storage ?? d.recommended));
      })
      .catch((e) => setError(String(e)));
  }, [sweep]);

  const r = useMemo(() => data?.results.find((x) => x.config === config) ?? null, [data, config]);

  if (error) return <main id="main-content" className="p-6 text-sm text-[var(--leo-bad)]">Could not load results: {error}</main>;
  if (!data || !r) return <main id="main-content" className="p-6 text-sm text-[var(--leo-text-dim)]">Loading results…</main>;

  const rel = r.reliability;
  const o = r.operator;
  const days = data.weeks.length * 7;
  const evRef = (data.assumptions.neighbourhood?.transformer_kva ?? 100) * 0.95;
  const maxNpv = Math.max(...r.sensitivity.map((s) => Math.abs(s.operator_npv)), Math.abs(o.npv)) || 1;

  return (
    <main id="main-content" className="mx-auto flex w-full max-w-[1180px] flex-col gap-6 px-6 pb-16 pt-6">
      <header>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold">Impact &amp; economics</h1>
          <div className="flex rounded-md border border-[var(--leo-border)] overflow-hidden text-sm" role="group" aria-label="Setting">
            {([["year", "Peri-urban · 100 kVA · 20% AC"], ["urban", "Urban · 200 kVA · 45% AC"]] as const).map(([k, label]) => (
              <button key={k} onClick={() => setSweep(k)} aria-pressed={sweep === k}
                className={`px-3 py-1 ${sweep === k ? "bg-[var(--leo-accent)]/20 text-[var(--leo-text)]" : "text-[var(--leo-text-dim)]"}`}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {data.scenario_name && <p className="text-xs text-[var(--leo-text-dim)]">{data.scenario_name}</p>}
        <p className="text-sm text-[var(--leo-text-dim)] max-w-3xl">
          {days} simulated days (one week in every month), same neighbourhood, weather, outages and household
          behaviour, with and without LEO. Figures are scaled to a year. Physical results come from the simulation;
          prices come from <code>economics.yaml</code>, and the ones marked <i>verify</i> are placeholders until sourced.
        </p>
        <div className="flex flex-wrap gap-2 mt-3" role="tablist" aria-label="LEO configuration">
          {data.results.map((x) => (
            <button
              key={x.config}
              role="tab"
              aria-selected={x.config === config}
              onClick={() => setConfig(x.config)}
              className={`text-sm rounded-full border px-3 py-1 ${
                x.config === config ? "border-[var(--leo-accent)] bg-[var(--leo-accent)]/15" : "border-[var(--leo-border)] text-[var(--leo-text-dim)]"
              }`}
            >
              {x.label}
              {x.config === data.recommended_storage && <span className="ml-1.5 text-xs text-[var(--leo-ok)]">best with storage</span>}
            </button>
          ))}
        </div>
      </header>

      <section>
        <h2 className="text-sm font-semibold mb-2">Reliability, measured against the no-LEO baseline</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Kpi
            title="Critical premises powered through outages"
            base="0%" leo={rel.critical_premise_availability_pct == null ? "—" : `${num(rel.critical_premise_availability_pct)}%`}
            better={rel.critical_premise_outage_hours_served ? `${num(rel.critical_premise_outage_hours_served)} premise-hours/yr kept on` : null}
            note={`${data.backup_premises.length} premises on the backup circuit (${[...new Set(data.backup_premises.map((p) => p.critical_class))].join(", ")}). Includes faults and announced load shedding; LEO pre-charges when the DISCOM gives notice.`}
          />
          <Kpi
            title="Transformer life at this loading"
            base={`${num(rel.dt_life_years_base, 1)}`} leo={(rel.dt_life_years ?? 0) > 100 ? "100+" : `${num(rel.dt_life_years, 1)}`} unit="years"
            better={rel.dt_failure_customer_hours_avoided ? `${num(rel.dt_failure_customer_hours_avoided)} customer-hours/yr of failure outages avoided` : null}
            note={`Expected failure rate ${num(rel.dt_failure_rate_pct_base, 1)}% → ${num(rel.dt_failure_rate_pct, 1)}% a year. BESCOM lost 7.96% of its DTs in FY 2023-24, 29% of them to overload; a 100 kVA unit costs ₹5.05 L to replace. Ageing doubles every ~6 °C of hot-spot temperature (IEEE C57.91), so shaving the evening peak buys back years.`}
          />
          <Kpi
            title="Evening peak at the transformer (average day)"
            base={num(rel.evening_peak_kw_mean_base)} leo={num(rel.evening_peak_kw_mean)} unit="kW"
            better={`${num(-pctChange(rel.evening_peak_kw_mean_base ?? 0, rel.evening_peak_kw_mean ?? 0))}% lower; worst day ${num(rel.evening_peak_kw_max_base)} → ${num(rel.evening_peak_kw_max)} kW`}
            note={`Rated ${num(data.assumptions.neighbourhood?.transformer_kva)} kVA (≈${num(evRef)} kW). Battery discharge is spread across the peak (water-filling); pumps are moved to midday every day and ACs are cycled on event days.`}
          />
          <Kpi
            title="Verified peak reduction (DFPO)"
            base="0" leo={num(r.physical.verified_peak_kw, 1)} unit="kW"
            better={`${rs((r.physical.verified_peak_kw ?? 0) * (data.assumptions.dfpo?.penalty_rs_per_kw_year ?? 0))}/yr of DFPO penalty the DISCOM avoids`}
            note="DFPO counts kW at the peak instant, not kWh. Measured here as the cut in this DT's evening peak on the year's most stressed days."
          />
          <Kpi
            title="Hours the transformer runs overloaded"
            base={num(rel.overload_hours_base)} leo={num(rel.overload_hours)} unit="h/yr"
            better={`Energy above rating ${num(rel.overload_kvah_base)} → ${num(rel.overload_kvah)} kVAh/yr`}
            note="This transformer has outgrown its rating, as many peri-urban DTs have. LEO reduces how far over it goes; it doesn't replace augmentation forever."
          />
          <Kpi
            title="Midday rooftop solar stored locally"
            base="0" leo={num(rel.solar_absorbed_kwh)} unit="kWh/yr"
            better={rel.solar_export_kwh ? `${num((100 * (rel.solar_absorbed_kwh ?? 0)) / (rel.solar_export_kwh ?? 1))}% of the neighbourhood's export` : null}
            note="The renewable-intermittency bridge: surplus PV absorbed at midday is what the battery spends on the evening ramp when solar collapses."
          />
          <Kpi
            title="Customer-hours of undervoltage"
            base={num(rel.undervoltage_customer_hours_base)} leo={num(rel.undervoltage_customer_hours)} unit="h/yr"
            better={null}
            note={`Only ${num(-pctChange(rel.undervoltage_customer_hours_base ?? 0, rel.undervoltage_customer_hours ?? 0))}% better: most of the drop is along the lines, which a battery at the transformer can't fix. LEO escalates a tap change to the DISCOM instead. Stated, not hidden.`}
          />
        </div>
      </section>

      <section className="grid gap-3 lg:grid-cols-2">
        <MonthlyChart rows={data.monthly} config={r.config} field="evening_peak_kw" label="Average evening peak by month"
          unit="kW" refLine={evRef} refLabel="transformer rating" />
        <MonthlyChart rows={data.monthly} config={r.config} field="aging_hours" label="Transformer life used in the sampled week"
          unit="equivalent hours" refLine={168} refLabel="normal ageing (168 h/week)" />
      </section>

      <section>
        <h2 className="text-sm font-semibold mb-2">Who gains what, per transformer per year</h2>
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 flex flex-col gap-3">
            <div>
              <h3 className="text-sm font-semibold">Operator (local cooperative / NGO aggregator)</h3>
              <p className="text-xs text-[var(--leo-text-dim)]">
                Runs a cluster of {data.assumptions.operating_model?.dts_per_operator} transformers with one technician.
                Pays {num(o.alpha * 100)}% of revenue to households.
              </p>
            </div>
            <Ledger title="Upfront capital" lines={Object.entries(o.capex)} total={o.capex_total} totalLabel="Total capex" />
            <Ledger
              title="Each year"
              lines={[...Object.entries(o.revenue), ["Paid to households", -o.payouts] as [string, number],
                ...Object.entries(o.opex).map(([k, v]) => [k, -v] as [string, number])]}
              total={o.net_cash_per_year} totalLabel="Net cash" tone={o.net_cash_per_year > 0 ? "ok" : "bad"}
            />
            <p className="text-sm">
              Payback <b>{o.payback_years ? `${num(o.payback_years, 1)} yr` : "never"}</b> · 10-yr NPV{" "}
              <b className={o.npv >= 0 ? "text-[var(--leo-ok)]" : "text-[var(--leo-bad)]"}>{rs(o.npv)}</b>
              {r.physical.battery_life_years != null && (
                <span className="text-xs text-[var(--leo-text-dim)]"> · packs last {num(r.physical.battery_life_years, 1)} yr
                  at {num(r.physical.efc_per_year)} cycles/yr (replacement included)</span>
              )}
            </p>
          </div>

          <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 flex flex-col gap-3">
            <div>
              <h3 className="text-sm font-semibold">DISCOM</h3>
              <p className="text-xs text-[var(--leo-text-dim)]">
                Must procure flexibility under Karnataka&apos;s DF/DSM Regulations 2026 or pay a penalty. Power purchase is
                priced hour by hour, so battery round-trip losses and DR rebound are already netted out.
              </p>
            </div>
            <dl className="grid grid-cols-2 gap-3 text-center">
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Gross saving, this DT</dt>
                <dd className="text-xl font-semibold text-[var(--leo-ok)]">{rs(r.discom.gross_saving)}<span className="text-xs font-normal">/yr</span></dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Per 1,000 overloaded DTs</dt>
                <dd className="text-xl font-semibold">{rs(r.discom.gross_saving * 1000)}<span className="text-xs font-normal">/yr</span></dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Technical losses saved</dt>
                <dd className="text-xl font-semibold">{num((rel.loss_kwh_base ?? 0) - (rel.loss_kwh ?? 0))}<span className="text-xs font-normal"> kWh/yr</span></dd>
                <dd className="text-xs text-[var(--leo-text-dim)]">{num(100 * (1 - (rel.loss_kwh ?? 0) / (rel.loss_kwh_base || 1)), 1)}% of this DT&apos;s I²R + transformer losses</dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Evening power not bought</dt>
                <dd className="text-xl font-semibold">{num(r.physical.flex_kwh / 1000, 1)}<span className="text-xs font-normal"> MWh/yr</span></dd>
                <dd className="text-xs text-[var(--leo-text-dim)]">at ₹10/kWh, refilled at ₹1.91 midday</dd></div>
            </dl>
            <Ledger title="Each year, vs no LEO"
              lines={Object.entries(r.discom).filter(([k]) => !["net", "max_rate_rs_per_kwh", "npv", "gross_saving"].includes(k)) as [string, number][]}
              total={r.discom.net} totalLabel="Net, after paying the operator" tone={r.discom.net > 0 ? "ok" : "bad"} />
            <p className="text-xs text-[var(--leo-text-dim)]">
              The net is what is left after the contract hands most of the saving to the operator, who funds the
              batteries, devices and household payments with it — the split is set inside the deal zone below.
            </p>
          </div>

          <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4 flex flex-col gap-3">
            <div>
              <h3 className="text-sm font-semibold">Households ({data.assumptions.neighbourhood?.households})</h3>
              <p className="text-xs text-[var(--leo-text-dim)]">
                Pay nothing upfront, bills unchanged, no penalties ever. Rewards are paid out of revenue the operator
                actually earned (§12.2).
              </p>
            </div>
            <dl className="grid grid-cols-2 gap-3 text-center">
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Upfront cost</dt><dd className="text-xl font-semibold">₹0</dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Average over all homes</dt><dd className="text-xl font-semibold">{rs(r.households.payout_per_household_rs)}<span className="text-xs font-normal">/yr</span></dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Backup for critical premises</dt><dd className="text-xl font-semibold">₹{num(data.assumptions.backup?.fee_rs_per_kwh)}<span className="text-xs font-normal">/kWh</span></dd></div>
              <div><dt className="text-xs text-[var(--leo-text-dim)]">Own home inverter instead</dt><dd className="text-xl font-semibold">₹15,000+<span className="text-xs font-normal"> per kWh</span></dd></div>
            </dl>
            {r.households.per_household && (() => {
              const ph = r.households.per_household;
              const pool = ph.streams_rs_per_household ?? 0;
              return (
                <table className="w-full text-xs">
                  <thead className="text-[var(--leo-text-dim)]"><tr><th className="text-left font-normal">Who</th><th className="text-right font-normal">Per year</th></tr></thead>
                  <tbody>
                    {(ph.n_pump_homes ?? 0) > 0 && (
                      <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">Pump home ({num(ph.n_pump_homes)}): fee + ToD saving + pool</td>
                        <td className="text-right font-semibold">{rs(ph.pump_home_fee_rs + ph.pump_home_tod_saving_rs + pool)}</td></tr>)}
                    {(ph.n_ac_homes ?? 0) > 0 && (
                      <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">AC home ({num(ph.n_ac_homes)}): event payments + pool</td>
                        <td className="text-right font-semibold">{rs(ph.ac_home_event_pay_rs + pool)}</td></tr>)}
                    <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">Every other home: solar / rebate pool</td>
                      <td className="text-right font-semibold">{rs(pool + (ph.sms_dr_rs_per_household ?? 0))}</td></tr>
                  </tbody>
                </table>
              );
            })()}
            <p className="text-xs text-[var(--leo-text-dim)]">
              ToD saving applies if KERC extends ToD to LT domestic (the amended Rights of Consumers Rules allow up to 20%).
              Tata Power-DDL&apos;s pilot paid ₹250/event (₹50/100 tiers too), 12–16 events a year.
            </p>
            <div className="text-xs">
              <p className="text-[var(--leo-text-dim)] mb-1">Demand response paid to households: {rs(r.physical.dr_paid_rs)}/yr</p>
              <table className="w-full">
                <tbody>
                  {(r.physical.smart_pumps ?? 0) > 0 && (
                    <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">Pump shifting (automated, daily)</td>
                      <td className="text-right">{num(r.physical.smart_pumps)} homes · ₹{num(data.assumptions.smart_dr?.pump_fee_rs_per_month)}/month</td></tr>)}
                  {(r.physical.smart_acs ?? 0) > 0 && (
                    <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">AC events (automated)</td>
                      <td className="text-right">{num(r.physical.ac_events)} events · {num(r.physical.ac_participations)} home-events · ₹75 each</td></tr>)}
                  <tr className="border-t border-[var(--leo-border)]/50"><td className="py-1">SMS offers (₹0 / 25 / 50 / 100)</td>
                    <td className="text-right">{["0", "25", "50", "100"].map((k) => num(r.physical.dr_offered_rs?.[k] ?? 0)).join(" / ")}</td></tr>
                </tbody>
              </table>
            </div>
            <p className="text-xs text-[var(--leo-text-dim)]">
              Operator cost per household: {rs(o.capex_total / 10 / 150 / 12 + o.opex_total / 150 / 12)}/month, recovered from
              the DISCOM&apos;s flexibility payments, not from households.
            </p>
          </div>
        </div>
      </section>

      <DealZone r={r} data={data} />

      <section className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
        <h3 className="text-sm font-semibold mb-1">What moves the operator&apos;s 10-year NPV</h3>
        <p className="text-xs text-[var(--leo-text-dim)] mb-3">
          Base case {rs(o.npv)}; DISCOM {rs(r.discom.net)}/yr. One assumption changed at a time. Prices the DISCOM
          pays or avoids move its side only, at a fixed DFPO rate.
        </p>
        <div className="flex flex-col gap-1.5">
          {r.sensitivity.map((s) => {
            const d = s.operator_npv - o.npv;
            return (
              <div key={s.case} className="grid grid-cols-[180px_1fr_90px_170px] items-center gap-2 text-xs">
                <span className="text-[var(--leo-text-dim)]">{s.case}</span>
                <div className="relative h-3 bg-[var(--leo-panel-raised)] rounded">
                  <div className="absolute top-0 bottom-0 w-px bg-[var(--leo-border)]" style={{ left: "50%" }} />
                  <div className={`absolute top-0 bottom-0 rounded ${d >= 0 ? "bg-[var(--leo-ok)]" : "bg-[var(--leo-bad)]"}`}
                    style={d >= 0 ? { left: "50%", width: `${(d / maxNpv) * 50}%` } : { right: "50%", width: `${(-d / maxNpv) * 50}%` }} />
                </div>
                <span className="text-right tabular-nums">{rs(s.operator_npv)}</span>
                <span className="text-right tabular-nums whitespace-nowrap text-[var(--leo-text-dim)]">DISCOM {rs(s.discom_net)}/yr</span>
              </div>
            );
          })}
        </div>
      </section>

      <Assumptions yaml={data.assumptions_yaml} />
    </main>
  );
}
