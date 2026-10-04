export type RunMeta = {
  run_id: string;
  label: string;
  leo_enabled: boolean;
  sim_start: string;
  sim_end: string;
  notes: string | null;
};

export type ForecastInterval = {
  ts_end: string;
  temperature_c: number | null;
  ghi_w_m2: number | null;
  trafo_pred_pct: number | null;
  trafo_actual_pct: number | null;
  phases: Record<string, { p10_kw: number; p50_kw: number; p90_kw: number; max_charge_kw: number | null; max_discharge_kw: number | null }>;
};

export type Forecast = {
  run_id: string;
  source_run: string;
  issued_at: string | null;
  model?: string;
  transformer_kva?: number;
  intervals: ForecastInterval[];
};

export type PredictedEvent = {
  type: "transformer_overload" | "undervoltage" | "overvoltage" | "load_shedding" | "unplanned_outage";
  phase: string | null;
  severity: "high" | "medium" | "low";
  title: string;
  predicted_at: string | null;
  start: string;
  end: string;
  worst_ts: string;
  why: string[];
  actual: { start: string; end: string; worst: number | null } | null;
  worst: number | null;
  missed?: boolean;
};

export type ActionItem = {
  ts: string;
  end: string | null;
  actor: string;
  kind: string;
  title: string;
  detail: string;
  reason: string;
  link: string | null;
  severity: "info" | "live" | "warn" | "bad";
  meta: Record<string, unknown>;
};

export const ms = (ts: string) => new Date(ts).getTime();

export function fmtTime(ts: string, withDay = false): string {
  return new Date(ts).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    ...(withDay ? { day: "2-digit", month: "short" } : {}),
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export const EVENT_STYLE: Record<string, { color: string; label: string }> = {
  transformer_overload: { color: "#e0473e", label: "Overload" },
  undervoltage: { color: "#e0a72e", label: "Undervoltage" },
  overvoltage: { color: "#9b7fe0", label: "Overvoltage" },
  load_shedding: { color: "#3ba9ff", label: "Load shedding" },
  unplanned_outage: { color: "#e0473e", label: "Outage" },
};
