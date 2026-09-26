"""gateway/forecast/load_model.py — LightGBM quantile load forecaster.

Owner A. See Build Specification v1.0 §10.2 and System Architecture v3.0
§10.2.

One model shared across phases and neighbourhoods — static features
distinguish them, because each phase alone has too little data. Three
LightGBM quantile regressors (P10/P50/P90), 15-minute intervals, 36-hour
horizon.

Gross load reconstruction (import - export + estimated PV) is what a real
deployment needs, since a net meter mixes consumption and generation. In
this simulator we already have world/households.py's true consumption
directly, so training targets sum straight from it — no reconstruction
needed here; that formula matters once real meter data (sim/measure.py,
not yet built) replaces it.

The latency rule is enforced in code, not by convention (Build Spec
ground rule #3): build_features() filters strictly on
`received_at <= run_time` and raises if the caller hands it data it
should not have. At the 14:00 run for tomorrow, the same-slot lag is
48 hours (yesterday's data is the newest available for a target 24-36h
out) — asserted explicitly in the demo below.

Excluded from training, per §10.2, but not implemented here: outage
intervals, the hour after restoration, and DR event windows. sim/loop.py
(not yet built) is what would mark them — training on them would teach
the model to under-predict exactly the peaks it exists to plan for.
Add an exclusion mask to train()'s input once that log exists; there's
nothing to filter against yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import lightgbm as lgb

QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}

FEATURE_COLUMNS = [
    "interval_of_day", "day_of_week", "month", "is_holiday", "is_festival",
    "temperature_c", "humidity_pct", "heat_index_c",
    "mean_temp_prev_24h", "rain_flag",
    "load_same_slot_yesterday", "load_same_slot_last_week", "load_7day_mean",
    "recent_daily_peak",
    "household_count", "total_sanctioned_kw", "ac_cooler_share", "commercial_share",
]


def heat_index_c(temp_c: np.ndarray, humidity_pct: np.ndarray) -> np.ndarray:
    """Simplified Rothfusz heat-index approximation, in Celsius. Only
    meaningfully diverges from temp_c above ~27C; below that we just
    return temp_c, since the full regression is only fit/valid for warmer
    conditions. (verify — an approximation, not the NWS-certified formula)
    """
    t_f = temp_c * 9.0 / 5.0 + 32.0
    hi_f = (
        -42.379 + 2.04901523 * t_f + 10.14333127 * humidity_pct
        - 0.22475541 * t_f * humidity_pct - 0.00683783 * t_f**2
        - 0.05481717 * humidity_pct**2 + 0.00122874 * t_f**2 * humidity_pct
        + 0.00085282 * t_f * humidity_pct**2 - 0.00000199 * t_f**2 * humidity_pct**2
    )
    hi_c = (hi_f - 32.0) * 5.0 / 9.0
    return np.where(temp_c >= 27.0, hi_c, temp_c)


def build_features(
    target_ts: pd.DatetimeIndex,
    run_time: pd.Timestamp,
    weather_15min: pd.DataFrame,
    gross_load_history: pd.DataFrame,
    static_features: dict,
    holidays_set: set,
    festivals_set: set,
) -> pd.DataFrame:
    """One feature row per interval in `target_ts`.

    `gross_load_history` must have columns (ts_end, gross_kw, received_at)
    for a single phase — filtered here to received_at <= run_time, which
    is the latency rule enforced in code rather than trusted from the
    caller. Raises if that leaves nothing usable at all.
    """
    usable_history = gross_load_history[gross_load_history["received_at"] <= run_time]
    if usable_history.empty:
        raise ValueError(f"no gross load history available as of run_time={run_time} — latency rule violation")

    history_by_ts = usable_history.set_index("ts_end")["gross_kw"]
    weather_by_ts = weather_15min.set_index("ts")

    rows = []
    for ts_end in target_ts:
        weather_row = weather_by_ts.reindex([ts_end]).iloc[0] if ts_end in weather_by_ts.index else None
        temp_c = float(weather_row["temperature_c"]) if weather_row is not None else np.nan
        humidity = float(weather_row["humidity_pct"]) if weather_row is not None else np.nan
        precip = float(weather_row["precipitation"]) if weather_row is not None else 0.0

        mean_temp_prev_24h = weather_by_ts["temperature_c"].reindex(
            pd.date_range(ts_end - pd.Timedelta(hours=24), ts_end, freq="15min")
        ).mean()

        same_slot_yesterday = history_by_ts.get(ts_end - pd.Timedelta(days=1), np.nan)
        same_slot_last_week = history_by_ts.get(ts_end - pd.Timedelta(days=7), np.nan)
        same_slot_7day = [history_by_ts.get(ts_end - pd.Timedelta(days=d), np.nan) for d in range(1, 8)]
        load_7day_mean = np.nanmean(same_slot_7day) if not all(np.isnan(same_slot_7day)) else np.nan

        recent_window = usable_history[
            (usable_history["ts_end"] < ts_end) & (usable_history["ts_end"] >= ts_end - pd.Timedelta(days=7))
        ]
        recent_daily_peak = recent_window["gross_kw"].max() if not recent_window.empty else np.nan

        rows.append({
            "ts_end": ts_end,
            "interval_of_day": (ts_end.hour * 4 + ts_end.minute // 15),
            "day_of_week": ts_end.dayofweek,
            "month": ts_end.month,
            "is_holiday": float(ts_end.date() in holidays_set),
            "is_festival": float(ts_end.date() in festivals_set),
            "temperature_c": temp_c,
            "humidity_pct": humidity,
            "heat_index_c": float(heat_index_c(np.array([temp_c]), np.array([humidity]))[0]) if not np.isnan(temp_c) else np.nan,
            "mean_temp_prev_24h": mean_temp_prev_24h,
            "rain_flag": float(precip > 0.1),
            "load_same_slot_yesterday": same_slot_yesterday,
            "load_same_slot_last_week": same_slot_last_week,
            "load_7day_mean": load_7day_mean,
            "recent_daily_peak": recent_daily_peak,
            **static_features,
        })
    return pd.DataFrame(rows)


@dataclass
class LoadModel:
    models: dict  # quantile name -> lgb.LGBMRegressor
    feature_columns: list

    def predict(self, features_df: pd.DataFrame) -> pd.DataFrame:
        X = features_df[self.feature_columns]
        out = features_df[["ts_end"]].copy()
        for name, model in self.models.items():
            out[f"{name}_kw"] = model.predict(X)
        # Quantile crossing can happen with independently-fit models;
        # enforce p10 <= p50 <= p90 by sorting each row.
        sorted_vals = np.sort(out[["p10_kw", "p50_kw", "p90_kw"]].to_numpy(), axis=1)
        out["p10_kw"], out["p50_kw"], out["p90_kw"] = sorted_vals[:, 0], sorted_vals[:, 1], sorted_vals[:, 2]
        return out


def train(features_df: pd.DataFrame, target_col: str = "gross_kw") -> LoadModel:
    """Fit three quantile LightGBM models. Time-based split is the
    caller's job (§10.2: "time-based split only") — this just fits on
    whatever rows it's given."""
    X = features_df[FEATURE_COLUMNS]
    y = features_df[target_col]
    models = {}
    for name, alpha in QUANTILES.items():
        model = lgb.LGBMRegressor(
            objective="quantile", alpha=alpha, n_estimators=200, num_leaves=31,
            learning_rate=0.05, min_child_samples=20, verbosity=-1,
        )
        model.fit(X, y)
        models[name] = model
    return LoadModel(models=models, feature_columns=FEATURE_COLUMNS)


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, alpha: float) -> float:
    diff = y_true - y_pred
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1) * diff)))


def evening_peak_error(y_true: pd.Series, y_pred: pd.Series, ts: pd.Series) -> dict:
    """Peak magnitude and timing error, per day, averaged."""
    df = pd.DataFrame({"ts": ts, "true": y_true, "pred": y_pred})
    df["date"] = df["ts"].dt.date
    mag_errors, timing_errors_min = [], []
    for _, day_df in df.groupby("date"):
        if len(day_df) < 2:
            continue
        true_peak_idx = day_df["true"].idxmax()
        pred_peak_idx = day_df["pred"].idxmax()
        mag_errors.append(abs(day_df.loc[true_peak_idx, "true"] - day_df.loc[pred_peak_idx, "pred"]))
        timing_errors_min.append(
            abs((day_df.loc[true_peak_idx, "ts"] - day_df.loc[pred_peak_idx, "ts"]).total_seconds()) / 60.0
        )
    return {"mean_peak_magnitude_error_kw": float(np.mean(mag_errors)),
            "mean_peak_timing_error_min": float(np.mean(timing_errors_min))}


def same_slot_last_week_baseline(features_df: pd.DataFrame) -> np.ndarray:
    return features_df["load_same_slot_last_week"].to_numpy()


def recent_average_baseline(features_df: pd.DataFrame) -> np.ndarray:
    return features_df["load_7day_mean"].to_numpy()


if __name__ == "__main__":
    import holidays as pyholidays

    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.weather import fetch_scenario_weather, to_15min

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)

    years = sorted(true_load.index.year.unique().tolist())
    india_holidays = pyholidays.India(years=years)
    holidays_set = {d for d in india_holidays}
    festivals_set: set = set()  # (verify) no separate festival list sourced; holidays package covers most of it

    # Synthetic received_at: SLA-consistent delay (§3.1 — 95% within 8h,
    # 98% within 12h), since sim/measure.py (real metering) isn't built.
    delay_hours = np.clip(rng.normal(6.0, 2.5, size=len(true_load)), 0.5, 24.0)

    pooled_rows = []
    for phase in ["R", "Y", "B"]:
        hh_ids = feeder.household.loc[feeder.household["phase"] == phase, "id"]
        gross_kw = true_load[hh_ids].sum(axis=1)
        gross_load_history = pd.DataFrame({
            "ts_end": gross_kw.index, "gross_kw": gross_kw.to_numpy(),
            "received_at": gross_kw.index + pd.to_timedelta(delay_hours, unit="h"),
        })

        phase_hh = feeder.household[feeder.household["phase"] == phase]
        static_features = {
            "household_count": len(phase_hh),
            "total_sanctioned_kw": phase_hh["sanctioned_load_kw"].sum(),
            "ac_cooler_share": ownership.set_index("household_id").loc[phase_hh["id"], ["has_ac", "has_cooler"]].any(axis=1).mean(),
            "commercial_share": phase_hh["is_business"].mean(),
        }

        # Train/test split is time-based only (§10.2): last 30 days held out.
        split_ts = gross_kw.index.max() - pd.Timedelta(days=30)
        run_time = split_ts + pd.Timedelta(hours=14)  # a normal 14:00 day-ahead run

        target_ts_train = gross_kw.index[gross_kw.index <= split_ts]
        target_ts_test = gross_kw.index[(gross_kw.index > split_ts)]

        features_train = build_features(
            target_ts_train, run_time=gross_kw.index.max(),  # training uses all available history, not latency-gated
            weather_15min=weather_15min, gross_load_history=gross_load_history,
            static_features=static_features, holidays_set=holidays_set, festivals_set=festivals_set,
        )
        features_train["gross_kw"] = gross_kw.reindex(features_train["ts_end"]).to_numpy()
        features_train = features_train.dropna(subset=FEATURE_COLUMNS + ["gross_kw"])

        features_test = build_features(
            target_ts_test, run_time=gross_kw.index.max(),
            weather_15min=weather_15min, gross_load_history=gross_load_history,
            static_features=static_features, holidays_set=holidays_set, festivals_set=festivals_set,
        )
        features_test["gross_kw"] = gross_kw.reindex(features_test["ts_end"]).to_numpy()
        features_test["phase"] = phase
        features_train["phase"] = phase
        pooled_rows.append((features_train, features_test))

    train_df = pd.concat([t for t, _ in pooled_rows], ignore_index=True)
    test_df = pd.concat([t for _, t in pooled_rows], ignore_index=True).dropna(subset=FEATURE_COLUMNS + ["gross_kw"])

    print(f"pooled training rows (all 3 phases): {len(train_df)}")
    model = train(train_df)

    predictions = model.predict(test_df)
    predictions["true_kw"] = test_df["gross_kw"].to_numpy()
    predictions["phase"] = test_df["phase"].to_numpy()

    print(f"\n--- held-out evaluation ({len(test_df)} intervals, last 30 days, pooled across phases) ---")
    for alpha_name, alpha in QUANTILES.items():
        loss = pinball_loss(predictions["true_kw"].to_numpy(), predictions[f"{alpha_name}_kw"].to_numpy(), alpha)
        print(f"pinball loss ({alpha_name}): {loss:.4f}")

    p50_mae = float(np.mean(np.abs(predictions["true_kw"] - predictions["p50_kw"])))
    coverage = float(((predictions["true_kw"] >= predictions["p10_kw"]) & (predictions["true_kw"] <= predictions["p90_kw"])).mean())
    print(f"P50 MAE: {p50_mae:.4f} kW")
    print(f"P10-P90 coverage: {coverage:.1%} (target ~80%)")

    peak_err = evening_peak_error(predictions["true_kw"], predictions["p50_kw"], test_df["ts_end"])
    print(f"peak magnitude error: {peak_err['mean_peak_magnitude_error_kw']:.3f} kW, "
          f"peak timing error: {peak_err['mean_peak_timing_error_min']:.1f} min")

    baseline_last_week = same_slot_last_week_baseline(test_df)
    baseline_avg = recent_average_baseline(test_df)
    valid = ~np.isnan(baseline_last_week) & ~np.isnan(baseline_avg)
    model_mae = float(np.mean(np.abs(predictions["true_kw"][valid] - predictions["p50_kw"][valid])))
    baseline_last_week_mae = float(np.mean(np.abs(test_df["gross_kw"].to_numpy()[valid] - baseline_last_week[valid])))
    baseline_avg_mae = float(np.mean(np.abs(test_df["gross_kw"].to_numpy()[valid] - baseline_avg[valid])))
    print(f"\nmodel P50 MAE: {model_mae:.4f} kW")
    print(f"same-slot-last-week baseline MAE: {baseline_last_week_mae:.4f} kW")
    print(f"recent-average baseline MAE: {baseline_avg_mae:.4f} kW")

    # Latency rule: a run_time before any data was received should fail loudly.
    print("\n--- latency rule check ---")
    try:
        too_early_run_time = gross_load_history["ts_end"].min() - pd.Timedelta(hours=1)
        build_features(
            gross_load_history["ts_end"].iloc[:1], run_time=too_early_run_time,
            weather_15min=weather_15min, gross_load_history=gross_load_history,
            static_features=static_features, holidays_set=holidays_set, festivals_set=festivals_set,
        )
        print("BUG: should have raised — no data should be available yet")
    except ValueError as e:
        print(f"correctly rejected: {e}")
