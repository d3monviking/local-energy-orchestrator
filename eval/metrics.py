"""eval/metrics.py — M1-M5 computation and season breakdown.

Owner A. See System Architecture v3.0 §15.3.

    M1 Overload trips avoided   — count and minutes of LV fuse trips from
                                   sustained phase overload
    M2 Voltage violation minutes — minutes per phase outside +-6% at the
                                   far-end sensor
    M3 Critical-premise availability — backup-served minutes / total
                                   critical-premise outage minutes
    M4 Evening peak reduction   — transformer peak kW, 18:00-22:00 IST,
                                   LEO vs baseline
    M5 Verified flexibility     — kWh verified against the DT's share of
                                   the 0.5% DFPO target

Baseline: the identical world under the same seed, weather, outage
schedule and appliance draws, with the battery blocks and DR disabled.
Both arms run over the same simulated days; reported by season.

These are pure functions over a truth-log DataFrame (see
eval/run_arms.py for how one arm's run produces it) — nothing here runs a
simulation itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

IST_OFFSET_HOURS = 5.5
EVENING_PEAK_HOURS_IST = (18, 22)

SEASON_BY_MONTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "summer", 4: "summer", 5: "summer",
    6: "monsoon", 7: "monsoon", 8: "monsoon", 9: "monsoon",
    10: "post_monsoon", 11: "post_monsoon",
}


def _ist_hour(ts: pd.Series) -> pd.Series:
    return (ts.dt.hour + ts.dt.minute / 60.0 + IST_OFFSET_HOURS) % 24


def season_of(ts: pd.Series) -> pd.Series:
    return ts.dt.month.map(SEASON_BY_MONTH)


def m1_overload_trips(truth_log: pd.DataFrame, interval_minutes: float = 15.0) -> pd.DataFrame:
    """`truth_log`: ts_end, phase, tripped (bool) — one row per
    bus-independent phase-interval (a trip is a phase-wide event once the
    fuse blows, not per-bus). Returns trip count and minutes per phase and
    season.
    """
    df = truth_log.sort_values(["phase", "ts_end"]).copy()
    df["season"] = season_of(df["ts_end"])
    # A trip "episode" is a maximal run of consecutive tripped intervals —
    # count rising edges (tripped now, not tripped the interval before),
    # not raw tripped-interval counts, so one long trip isn't miscounted
    # as many trips.
    df["prev_tripped"] = df.groupby("phase")["tripped"].shift(1, fill_value=False)
    df["episode_start"] = df["tripped"] & ~df["prev_tripped"]

    return (
        df.groupby(["season", "phase"])
        .agg(trip_minutes=("tripped", lambda s: s.sum() * interval_minutes), trip_count=("episode_start", "sum"))
        .reset_index()
    )


def m2_voltage_violation_minutes(
    truth_log: pd.DataFrame, v_limit_pct: float, nominal_v_ln: float, interval_minutes: float = 15.0
) -> pd.DataFrame:
    """`truth_log`: ts_end, phase, far_end_voltage_v — far-end sensor
    reading only (§15.3 is explicit this is measured at the far-end
    sensor, not just anywhere on the phase)."""
    df = truth_log.copy()
    df["season"] = season_of(df["ts_end"])
    lower = nominal_v_ln * (1 - v_limit_pct / 100.0)
    upper = nominal_v_ln * (1 + v_limit_pct / 100.0)
    df["violating"] = (df["far_end_voltage_v"] < lower) | (df["far_end_voltage_v"] > upper)
    return (
        df.groupby(["season", "phase"])["violating"]
        .agg(violation_minutes=lambda s: s.sum() * interval_minutes)
        .reset_index()
    )


def m3_critical_premise_availability(outage_log: pd.DataFrame) -> pd.DataFrame:
    """`outage_log`: household_id, ts, outage_active (bool), backup_served
    (bool) — one row per registered critical premise per interval during
    outages only (rows where outage_active is False don't count toward
    either the numerator or denominator).
    """
    df = outage_log[outage_log["outage_active"]].copy()
    if df.empty:
        return pd.DataFrame(columns=["household_id", "total_outage_intervals", "backup_served_intervals", "availability"])
    grouped = df.groupby("household_id").agg(
        total_outage_intervals=("outage_active", "sum"),
        backup_served_intervals=("backup_served", "sum"),
    )
    grouped["availability"] = grouped["backup_served_intervals"] / grouped["total_outage_intervals"]
    return grouped.reset_index()


def m4_evening_peak_reduction(
    truth_log_leo: pd.DataFrame, truth_log_baseline: pd.DataFrame
) -> pd.DataFrame:
    """`truth_log_*`: ts_end, transformer_kw — aggregate transformer
    loading (all phases). Peak is taken over the 18:00-22:00 IST window,
    per day, per season, then averaged.
    """
    def evening_peaks(df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["ist_hour"] = _ist_hour(d["ts_end"])
        d["date"] = d["ts_end"].dt.date
        d["season"] = season_of(d["ts_end"])
        evening = d[(d["ist_hour"] >= EVENING_PEAK_HOURS_IST[0]) & (d["ist_hour"] < EVENING_PEAK_HOURS_IST[1])]
        return evening.groupby(["season", "date"])["transformer_kw"].max().reset_index()

    leo_peaks = evening_peaks(truth_log_leo).rename(columns={"transformer_kw": "leo_peak_kw"})
    baseline_peaks = evening_peaks(truth_log_baseline).rename(columns={"transformer_kw": "baseline_peak_kw"})
    merged = leo_peaks.merge(baseline_peaks, on=["season", "date"])
    merged["reduction_kw"] = merged["baseline_peak_kw"] - merged["leo_peak_kw"]

    return merged.groupby("season").agg(
        mean_leo_peak_kw=("leo_peak_kw", "mean"),
        mean_baseline_peak_kw=("baseline_peak_kw", "mean"),
        mean_reduction_kw=("reduction_kw", "mean"),
    ).reset_index()


def m5_verified_flexibility(dispatch_log: pd.DataFrame, dt_dfpo_target_kwh: float, interval_hours: float = 0.25) -> dict:
    """`dispatch_log`: ts_end, phase, setpoint_kw, in_flexibility_window
    (bool) — kWh discharged during the committed flexibility delivery
    window. This is what real DFPO settlement actually verifies: delivered
    kWh against baseline during the committed window, not "did it happen
    to avert an imminent local violation this exact interval" — a well-
    provisioned feeder can deliver real, valuable flexibility on a day
    with zero local violations at all. This is a battery-only lower
    bound: a complete M5 also counts DR-engine-verified household
    reductions (cloud/dr_engine, not yet built).
    """
    served = dispatch_log[(dispatch_log["setpoint_kw"] > 0) & dispatch_log["in_flexibility_window"]]
    verified_kwh = float((served["setpoint_kw"] * interval_hours).sum())
    return {
        "verified_kwh": verified_kwh,
        "dt_dfpo_target_kwh": dt_dfpo_target_kwh,
        "share_of_target": verified_kwh / dt_dfpo_target_kwh if dt_dfpo_target_kwh > 0 else float("nan"),
    }


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    ts = pd.date_range("2026-04-01", periods=96 * 5, freq="15min", tz="UTC")

    truth_log = pd.DataFrame({
        "ts_end": np.tile(ts, 3),
        "phase": np.repeat(["R", "Y", "B"], len(ts)),
        "tripped": rng.random(len(ts) * 3) < 0.01,
        "far_end_voltage_v": rng.normal(248, 6, len(ts) * 3),
        "transformer_kw": np.tile(30 + 20 * np.sin(np.linspace(0, 10 * np.pi, len(ts))), 3) + rng.normal(0, 2, len(ts) * 3),
    })
    print("--- M1 ---")
    print(m1_overload_trips(truth_log))
    print("\n--- M2 ---")
    print(m2_voltage_violation_minutes(truth_log, v_limit_pct=6.0, nominal_v_ln=250.0))

    outage_log = pd.DataFrame({
        "household_id": ["HH-048"] * 20,
        "ts": pd.date_range("2026-04-01 19:00", periods=20, freq="15min", tz="UTC"),
        "outage_active": [True] * 20,
        "backup_served": [True] * 14 + [False] * 6,
    })
    print("\n--- M3 ---")
    print(m3_critical_premise_availability(outage_log))

    baseline_log = truth_log[truth_log["phase"] == "R"].copy()
    leo_log = baseline_log.copy()
    leo_log["transformer_kw"] *= 0.85  # pretend LEO shaved 15% off transformer loading
    print("\n--- M4 ---")
    print(m4_evening_peak_reduction(leo_log, baseline_log))

    dispatch_log = pd.DataFrame({
        "ts_end": ts[:20], "phase": "R", "setpoint_kw": [7.5] * 15 + [0.0] * 5,
        "in_flexibility_window": [True] * 15 + [False] * 5,
    })
    print("\n--- M5 ---")
    print(m5_verified_flexibility(dispatch_log, dt_dfpo_target_kwh=50.0))
