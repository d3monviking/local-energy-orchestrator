"""cloud/settlement/streams.py — Stream 1 absorption, Stream 2 rebate.

Owner A. See Build Specification v1.0 §12.3, §12.4 and System Architecture
v3.0 §12.3, §12.4.

Stream 1 (surplus absorption) is interval-level and directly measurable:
when a battery block charges, pay exporting households on that phase
pro-rata by export share. No ranking involved.

Stream 2 (discharge rebate) reaches specific households from one daily
total per phase, via the five-step allocation in §12.4: net deficit
against surplus on paper (no physical transfer), rank the remainder by
recent Stream 1 participation with a fairness nudge for persistent
near-misses, then hand out the day's discharge in ranking order up to a
per-household cap, the last household matched partially.

Neither stream ever produces a negative amount — there is no penalty
mechanism anywhere in LEO.
"""

from __future__ import annotations

import pandas as pd

# (verify) small nudge, same units as the (already-decayed) Stream 1
# participation score — §12.4's "adjustment so a household that keeps
# narrowly missing out does not stay at the back."
FAIRNESS_BOOST_PER_NEAR_MISS = 0.05


def stream1_absorption_payments(
    dispatch_df: pd.DataFrame,
    export_df: pd.DataFrame,
    feed_in_tariff_paise_per_kwh: float,
    tod_rate_paise_per_kwh: float,
    interval_hours: float = 0.25,
) -> pd.DataFrame:
    """`dispatch_df`: run_id, phase, ts_end, setpoint_kw (negative =
    charging). `export_df`: household_id, phase, ts_end, export_kwh.
    Returns ledger-shaped rows (household_id, date, entry_type='absorption_payment',
    matched_kwh, amount_paise).
    """
    rate = (feed_in_tariff_paise_per_kwh + tod_rate_paise_per_kwh) / 2.0
    charging = dispatch_df[dispatch_df["setpoint_kw"] < 0]

    rows = []
    for _, d in charging.iterrows():
        charge_kwh = -d["setpoint_kw"] * interval_hours
        interval_exports = export_df[(export_df["phase"] == d["phase"]) & (export_df["ts_end"] == d["ts_end"])]
        total_export_kwh = interval_exports["export_kwh"].sum()
        if total_export_kwh <= 0:
            continue

        payable_kwh = min(charge_kwh, total_export_kwh)  # capped by the block's actual charge
        for _, e in interval_exports.iterrows():
            if e["export_kwh"] <= 0:
                continue
            share = e["export_kwh"] / total_export_kwh
            matched_kwh = share * payable_kwh
            amount_paise = round(matched_kwh * rate)
            if amount_paise <= 0:
                continue
            rows.append({
                "household_id": e["household_id"], "date": d["ts_end"].date(),
                "entry_type": "absorption_payment", "deficit_kwh": None,
                "matched_kwh": matched_kwh, "amount_paise": int(amount_paise),
                "linked_event_id": None, "rank_snapshot": None,
            })
    return pd.DataFrame(rows)


def stream2_discharge_rebate(
    date,
    deficit_kwh: pd.Series,
    stream1_participation_score: pd.Series,
    near_miss_streak: pd.Series,
    battery_daily_discharge_kwh: float,
    pool_rate_paise_per_kwh: float,
) -> pd.DataFrame:
    """The five-step allocation. `deficit_kwh` (Step A: each household's
    metered import for the day) and `stream1_participation_score` (recent
    Stream 1 kWh relative to normal usage, already decayed by the caller)
    are indexed by household_id. `near_miss_streak` counts consecutive
    recent days a household ranked just outside the cutoff.

    Step B (netting local surplus against deficit "on paper") doesn't
    change any number here — no physical transfer happens, so it's a
    bookkeeping statement about what the battery+grid had to cover, not a
    transformation of deficit_kwh itself.
    """
    per_household_cap_kwh = battery_daily_discharge_kwh / 15.0  # §12.4: why the cap

    rank_score = stream1_participation_score.add(
        FAIRNESS_BOOST_PER_NEAR_MISS * near_miss_streak, fill_value=0.0
    )
    ranking = rank_score.sort_values(ascending=False)

    remaining_pool_kwh = battery_daily_discharge_kwh
    rows = []
    for rank, household_id in enumerate(ranking.index, start=1):
        if remaining_pool_kwh <= 0:
            break
        hh_deficit = float(deficit_kwh.get(household_id, 0.0))
        if hh_deficit <= 0:
            continue
        matched_kwh = min(hh_deficit, per_household_cap_kwh, remaining_pool_kwh)  # Step D, incl. partial match
        if matched_kwh <= 0:
            continue
        remaining_pool_kwh -= matched_kwh
        amount_paise = round(matched_kwh * pool_rate_paise_per_kwh)
        if amount_paise <= 0:
            continue
        rows.append({
            "household_id": household_id, "date": date, "entry_type": "discharge_rebate",
            "deficit_kwh": hh_deficit, "matched_kwh": matched_kwh, "amount_paise": int(amount_paise),
            "rank_snapshot": rank, "linked_event_id": None,
        })
    # Step E: everyone else's deficit is left as ordinary usage — no row.
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import numpy as np
    import pandas as pd

    ts = pd.Timestamp("2026-08-29 13:00:00Z")
    dispatch_df = pd.DataFrame([
        {"run_id": "normal", "phase": "R", "ts_end": ts, "setpoint_kw": -4.0},
    ])
    export_df = pd.DataFrame([
        {"household_id": "HH-001", "phase": "R", "ts_end": ts, "export_kwh": 0.6},
        {"household_id": "HH-002", "phase": "R", "ts_end": ts, "export_kwh": 0.4},
        {"household_id": "HH-003", "phase": "Y", "ts_end": ts, "export_kwh": 1.0},  # different phase, not paid
    ])
    stream1 = stream1_absorption_payments(dispatch_df, export_df, feed_in_tariff_paise_per_kwh=250, tod_rate_paise_per_kwh=700)
    print("--- Stream 1 ---")
    print(stream1)
    assert (stream1["amount_paise"] >= 0).all()

    rng = np.random.default_rng(1)
    n = 20
    deficit = pd.Series(rng.uniform(0.5, 4.0, n), index=[f"HH-{i:03d}" for i in range(n)])
    participation = pd.Series(rng.uniform(0, 1, n), index=deficit.index)
    near_miss = pd.Series(rng.integers(0, 4, n), index=deficit.index)
    stream2 = stream2_discharge_rebate(
        date="2026-08-29", deficit_kwh=deficit, stream1_participation_score=participation,
        near_miss_streak=near_miss, battery_daily_discharge_kwh=40.0, pool_rate_paise_per_kwh=600,
    )
    print("\n--- Stream 2 ---")
    print(stream2)
    print(f"\nhouseholds reached: {len(stream2)} / {n}")
    print(f"total matched kWh: {stream2['matched_kwh'].sum():.2f} (battery discharge: 40.0 kWh)")
    assert stream2["matched_kwh"].sum() <= 40.0 + 1e-6
    assert (stream2["amount_paise"] >= 0).all()
    assert (stream2["matched_kwh"] <= 40.0 / 15.0 + 1e-6).all()
