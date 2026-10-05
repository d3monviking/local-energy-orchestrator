"""gateway/orchestrator/irt.py — similarity blend of historical trajectories.

Owner A. See Build Specification v1.0 §5.3 and System Architecture v3.0
§9.1.

Finds historical days similar to tomorrow and blends their perfect-
foresight trajectories (world/irt_library.py's output) into a single
reference SoC trajectory per phase. Similarity is judged only on what's
known a day ahead — forecast daily mean/max temperature, a clear-sky
index, day type, and festival flag — never on household load, which
isn't available in time to use this way (§9.1).

Gaussian kernel on standardised numeric features (temperature, clear-sky
index) plus a fixed penalty for a day-type or festival-flag mismatch;
blend the top-10 nearest days' trajectories, weighted by kernel value.

This is a per-request lookup against the library, not a fit — no model
is trained or persisted here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

TOP_N = 10
KERNEL_BANDWIDTH = 1.0
CATEGORICAL_MISMATCH_PENALTY = 2.0  # in squared standardised-distance units

NUMERIC_FEATURES = ["mean_temp_c", "max_temp_c", "clear_sky_index"]


def compute_daily_features(weather_15min: pd.DataFrame, lat: float, lon: float) -> pd.DataFrame:
    """Per-day similarity features from a 15-minute weather series (either
    a full historical year, to tag the library, or a single forecast day).

    clear_sky_index = actual GHI / clear-sky GHI, integrated over the day
    — how much of the day's theoretical maximum sun the day actually got,
    a compact stand-in for "clear vs overcast" that a day-ahead forecast
    can estimate.
    """
    location = pvlib.location.Location(lat, lon, tz="UTC")
    idx = pd.DatetimeIndex(weather_15min["ts"])
    clearsky_ghi = location.get_clearsky(idx, model="ineichen")["ghi"].to_numpy()

    df = weather_15min.copy()
    df["date"] = df["ts"].dt.date.astype(str)
    df["clearsky_ghi"] = clearsky_ghi

    daily = df.groupby("date").agg(
        mean_temp_c=("temperature_c", "mean"),
        max_temp_c=("temperature_c", "max"),
        ghi_sum=("ghi_w_m2", "sum"),
        clearsky_ghi_sum=("clearsky_ghi", "sum"),
    ).reset_index()
    daily["clear_sky_index"] = (
        daily["ghi_sum"] / daily["clearsky_ghi_sum"].replace(0, np.nan)
    ).clip(0, 1.2).fillna(0.0)

    dates = pd.to_datetime(daily["date"])
    daily["is_weekend"] = dates.dt.dayofweek >= 5

    years = sorted(dates.dt.year.unique().tolist())
    import holidays as pyholidays
    calendar = pyholidays.India(years=years)
    daily["is_festival"] = dates.dt.date.astype(str).isin({str(d) for d in calendar})

    return daily[["date", "mean_temp_c", "max_temp_c", "clear_sky_index", "is_weekend", "is_festival"]]


def _standardize(df: pd.DataFrame, stats: pd.DataFrame) -> np.ndarray:
    return ((df[NUMERIC_FEATURES] - stats.loc["mean"]) / stats.loc["std"].replace(0, 1)).to_numpy()


def blend_reference_trajectory(
    target_features: pd.Series,
    library_features: pd.DataFrame,
    irt_library: pd.DataFrame,
    phase: str,
    top_n: int = TOP_N,
    bandwidth: float = KERNEL_BANDWIDTH,
    exclude_date: str | None = None,
) -> tuple[np.ndarray, pd.DataFrame]:
    """The IRT reference trajectory for `phase`: a Gaussian-kernel-weighted
    blend of the top-`n` historical days most similar to `target_features`.

    `exclude_date` drops one date from the candidate pool — used for
    leave-one-out validation (a day can't be blended from itself).

    Returns (96-length SoC-fraction array, the chosen days with their
    weights, for auditability).
    """
    candidates = library_features
    if exclude_date is not None:
        candidates = candidates[candidates["date"] != exclude_date]

    stats = candidates[NUMERIC_FEATURES].agg(["mean", "std"])
    cand_z = _standardize(candidates, stats)
    target_z = _standardize(pd.DataFrame([target_features]), stats)[0]

    numeric_dist_sq = ((cand_z - target_z) ** 2).sum(axis=1)
    weekend_mismatch = (candidates["is_weekend"].to_numpy() != bool(target_features["is_weekend"]))
    festival_mismatch = (candidates["is_festival"].to_numpy() != bool(target_features["is_festival"]))
    dist_sq = (
        numeric_dist_sq
        + CATEGORICAL_MISMATCH_PENALTY * weekend_mismatch
        + CATEGORICAL_MISMATCH_PENALTY * festival_mismatch
    )

    kernel_weight = np.exp(-dist_sq / (2 * bandwidth ** 2))
    top_positions = np.argsort(-kernel_weight)[:top_n]
    chosen = candidates.iloc[top_positions].copy()
    chosen["weight"] = kernel_weight[top_positions]
    chosen["weight"] /= chosen["weight"].sum()

    phase_lib = irt_library[irt_library["phase"] == phase].copy()
    phase_lib["date"] = phase_lib["date"].astype(str)
    pivot = phase_lib.pivot(index="date", columns="interval", values="soc_frac")

    trajectory = np.zeros(96)
    for _, row in chosen.iterrows():
        if row["date"] in pivot.index:
            trajectory += row["weight"] * pivot.loc[row["date"]].to_numpy()

    return trajectory, chosen[["date", "weight"]].reset_index(drop=True)


if __name__ == "__main__":
    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.pv_truth import assign_pv_truth_params, generate_true_pv
    from world.weather import fetch_scenario_weather, to_15min
    from world.irt_library import build_library
    import numpy as np

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])

    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)
    irt_library = build_library(
        feeder.household, load, pv, scenario["battery_blocks"], rng,
        temperature_c=weather_15min["temperature_c"].to_numpy(),
    )

    daily_features = compute_daily_features(weather_15min, lat, lon)
    print(f"daily features for {len(daily_features)} days")
    print(daily_features.head())

    # Leave-one-out validation: for a handful of test days, exclude them
    # from the library, blend a reference from the OTHER days using their
    # own true weather as a forecast stand-in, and check the blend is
    # closer to that day's own perfect-foresight trajectory than either a
    # random single day or the flat all-day average is.
    phase = "R"
    phase_lib = irt_library[irt_library.phase == phase].copy()
    phase_lib["date"] = phase_lib["date"].astype(str)
    pivot = phase_lib.pivot(index="date", columns="interval", values="soc_frac")
    mean_trajectory = pivot.mean(axis=0).to_numpy()

    test_dates = daily_features["date"].sample(n=80, random_state=42).tolist()
    blend_errors, mean_baseline_errors, random_baseline_errors = [], [], []
    rng2 = np.random.default_rng(7)

    for test_date in test_dates:
        if test_date not in pivot.index:
            continue
        target = daily_features[daily_features["date"] == test_date].iloc[0]
        truth = pivot.loc[test_date].to_numpy()

        blended, chosen = blend_reference_trajectory(
            target, daily_features, irt_library, phase, exclude_date=test_date
        )
        blend_errors.append(np.sqrt(np.mean((blended - truth) ** 2)))
        mean_baseline_errors.append(np.sqrt(np.mean((mean_trajectory - truth) ** 2)))

        other_dates = [d for d in pivot.index if d != test_date]
        random_date = rng2.choice(other_dates)
        random_errors_traj = pivot.loc[random_date].to_numpy()
        random_baseline_errors.append(np.sqrt(np.mean((random_errors_traj - truth) ** 2)))

    print(f"\nleave-one-out validation over {len(blend_errors)} test days (phase {phase}):")
    print(f"  IRT similarity blend  RMSE: {np.mean(blend_errors):.4f}")
    print(f"  flat all-day average  RMSE: {np.mean(mean_baseline_errors):.4f}")
    print(f"  random single day     RMSE: {np.mean(random_baseline_errors):.4f}")
    print(f"  (lower is better; the blend should beat both naive baselines)")

    example_target = daily_features.iloc[len(daily_features) // 2]
    _, chosen = blend_reference_trajectory(example_target, daily_features, irt_library, phase)
    print(f"\ntop-{TOP_N} days blended for {example_target['date']} "
          f"(mean_temp={example_target['mean_temp_c']:.1f}C, clear_sky_index={example_target['clear_sky_index']:.2f}):")
    print(chosen.merge(daily_features, on="date")[
        ["date", "weight", "mean_temp_c", "clear_sky_index", "is_weekend", "is_festival"]
    ].round(3))
