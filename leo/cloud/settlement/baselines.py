"""cloud/settlement/baselines.py — per-household and holdout baselines.

Owner A. See Build Specification v1.0 §12.5 and System Architecture v3.0
§12.5.

Two verification methods, matching Karnataka's DF/DSM regulations'
categories: a random holdout comparison for programme-level impact, and a
per-household baseline (IPMVP Option C style) that drives individual DR
payments. Formulas are meant to be publishable so an empanelled
verification agency can reproduce them from DISCOM meter data (§12.5).
"""

from __future__ import annotations

from datetime import date as date_type

import numpy as np
import pandas as pd

N_SIMILAR_DAYS = 10
PRE_EVENT_ADJUSTMENT_CLIP = (0.5, 1.5)  # (verify) sanity bounds on the day-of scaling factor


def per_household_baseline(
    meter_history: pd.DataFrame,
    household_id: str,
    event_date: date_type,
    window: tuple[str, str],
    pre_event_window: tuple[str, str],
    event_dates: set,
    n_similar_days: int = N_SIMILAR_DAYS,
) -> float:
    """IPMVP Option C baseline for one household's DR event window: the
    mean of that window's usage over the most recent `n_similar_days`
    non-event days matching this event day's day-of-week (to control for
    the weekly rhythm), scaled by the ratio of this event day's own
    pre-event usage to those same days' pre-event usage — so a baseline
    from a cool week doesn't overstate savings on an unusually hot event
    day, or vice versa.

    `meter_history` must already be day-late filtered (received_at <=
    settlement run time) by the caller — this function has no visibility
    into the latency rule.
    """
    hh = meter_history[meter_history["household_id"] == household_id].copy()
    hh["date"] = hh["ts_end"].dt.date
    hh["time"] = hh["ts_end"].dt.time
    target_dow = pd.Timestamp(event_date).dayofweek

    candidate_days = sorted(
        {
            d for d in hh["date"].unique()
            if d < event_date and pd.Timestamp(d).dayofweek == target_dow and d not in event_dates
        },
        reverse=True,
    )[:n_similar_days]

    def window_sum(day, w: tuple[str, str]) -> float:
        start_t, end_t = pd.Timestamp(w[0]).time(), pd.Timestamp(w[1]).time()
        mask = (hh["date"] == day) & (hh["time"] >= start_t) & (hh["time"] < end_t)
        return float(hh.loc[mask, "import_kwh"].sum())

    if not candidate_days:
        return 0.0

    baseline_window_kwh = float(np.mean([window_sum(d, window) for d in candidate_days]))
    baseline_pre_kwh = float(np.mean([window_sum(d, pre_event_window) for d in candidate_days]))
    event_pre_kwh = window_sum(event_date, pre_event_window)

    adjustment = (event_pre_kwh / baseline_pre_kwh) if baseline_pre_kwh > 0 else 1.0
    adjustment = float(np.clip(adjustment, *PRE_EVENT_ADJUSTMENT_CLIP))
    return baseline_window_kwh * adjustment


def verified_reduction_kwh(
    baseline_kwh: float, actual_kwh: float, cap_kwh: float | None = None
) -> tuple[float, float]:
    """Raw reduction (for the bandit's learning signal, which uses raw
    values including negatives to avoid bias) and payable reduction
    (clamped to zero-or-positive, capped, per §11.4/§5.4: payment only for
    positive reductions)."""
    raw = baseline_kwh - actual_kwh
    payable = max(0.0, raw)
    if cap_kwh is not None:
        payable = min(payable, cap_kwh)
    return raw, payable


def holdout_comparison(
    offered_baseline_kwh: pd.Series, offered_actual_kwh: pd.Series,
    holdout_baseline_kwh: pd.Series, holdout_actual_kwh: pd.Series,
) -> dict:
    """Programme-level impact: mean reduction for offered vs held-out
    households. Large-scale comparison for a homogeneous group, per
    Karnataka's two verification categories (§12.5)."""
    offered_reduction = float((offered_baseline_kwh - offered_actual_kwh).mean())
    holdout_reduction = float((holdout_baseline_kwh - holdout_actual_kwh).mean())
    return {
        "offered_mean_reduction_kwh": offered_reduction,
        "holdout_mean_reduction_kwh": holdout_reduction,
        "programme_effect_kwh": offered_reduction - holdout_reduction,
    }


if __name__ == "__main__":
    import numpy as np
    from datetime import timedelta

    rng = np.random.default_rng(0)
    dates = [date_type(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    rows = []
    for d in dates:
        for hour, minute in [(t // 4, (t % 4) * 15) for t in range(96)]:
            ts_end = pd.Timestamp(d) + pd.Timedelta(hours=hour, minutes=minute + 15)
            hour_factor = 1.0 + 0.5 * np.exp(-((hour - 19.5) ** 2) / (2 * 1.5 ** 2))
            noise = rng.normal(1.0, 0.1)
            rows.append({"household_id": "HH-001", "ts_end": ts_end, "import_kwh": 0.05 * hour_factor * noise})
    meter_history = pd.DataFrame(rows)

    event_date = date_type(2026, 8, 29)
    baseline = per_household_baseline(
        meter_history, "HH-001", event_date, window=("19:00", "21:00"),
        pre_event_window=("17:00", "19:00"), event_dates=set(),
    )
    print(f"baseline for {event_date} 19:00-21:00 window: {baseline:.3f} kWh")

    actual_kwh = baseline * 0.7  # pretend the household cut 30%
    raw, payable = verified_reduction_kwh(baseline, actual_kwh, cap_kwh=2.0)
    print(f"actual={actual_kwh:.3f} kWh -> raw reduction={raw:.3f} kWh, payable={payable:.3f} kWh")

    result = holdout_comparison(
        pd.Series([baseline] * 20), pd.Series([actual_kwh] * 20),
        pd.Series([baseline] * 5), pd.Series([baseline * 0.98] * 5),
    )
    print("holdout comparison:", result)
