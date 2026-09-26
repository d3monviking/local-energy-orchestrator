"""gateway/forecast/pv_model.py — physics PV forecast + four-pass calibration.

Owner A. See Build Specification v1.0 §5.2, §10.3.

This is the FORECAST-side PV model — deliberately different from
world/pv_truth.py, which is the simulator's ground truth (real, hidden
per-household tilt/azimuth/soiling). This file assumes ONE fixed tilt and
azimuth for every PV household in a neighbourhood (§5.2: "Assumed inputs:
Tilt 10-15°, azimuth due south"), because that's genuinely all a real
deployment would know, and fits a single per-neighbourhood scale factor k
to make up the difference — a rooftop system behind a net meter is never
directly measured, only net import/export is.

Chain: solar position -> POA irradiance (Perez, using GHI/DNI/DHI
directly, so no Erbs decomposition is needed, same as world/pv_truth.py)
-> cell temperature (Faiman) -> DC (PVWatts) -> AC (PVWatts inverter
model) -> wiring loss, computed PER UNIT KWP so it can be scaled by k and
by each household's installed capacity.

Four-pass calibration (§5.2): the naive fit is circular (the load model
needs the PV estimate to reconstruct gross load, but the PV estimate
needs the load model to separate consumption from generation), so:
  Pass 0  A per-sanctioned-kW load shape from non-PV households, scaled to
          PV households' own sanctioned load, as an initial load estimate.
  Pass 1  On clear-sky midday intervals, k = sum(initial_load_est -
          net_import) / sum(unit PV model output x installed kWp).
  Pass 2  Reconstruct gross load with k; train the phase-level load
          forecaster (gateway/forecast/load_model.py) on it.
  Pass 3  Refit k using the trained model's own predictions, apportioned
          back to each PV household by sanctioned load (same style as
          Pass 0), in place of the naive Pass-0 estimate. Stop.
Monthly refits run Pass 3 alone.

Since the simulator knows true PV, k_fitted / k_true is reported as a
validation metric (§5.2) — a validation-only capability that obviously
doesn't exist in real deployment.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from gateway.forecast.load_model import build_features, train as train_load_model, FEATURE_COLUMNS

ASSUMED_TILT_DEG = 12.5      # midpoint of the assumed 10-15 deg range
ASSUMED_AZIMUTH_DEG = 180.0  # due south
WIRING_LOSS = 0.02
TEMP_COEFF_PDC = -0.0037

CLEAR_SKY_INDEX_THRESHOLD = 0.85  # (verify) "clear-sky midday intervals" cutoff
MIDDAY_HOURS_IST = (10, 15)
IST_OFFSET_HOURS = 5.5


def unit_pv_output_kw(lat: float, lon: float, weather_15min: pd.DataFrame) -> pd.Series:
    """AC output per 1 kWp installed, using the ASSUMED tilt/azimuth —
    not each household's true (hidden) values. This is exactly what the
    scale factor k multiplies."""
    times = pd.DatetimeIndex(weather_15min["ts"])
    solar_position = pvlib.solarposition.get_solarposition(times, lat, lon)
    dni_extra = pvlib.irradiance.get_extra_radiation(times)
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=ASSUMED_TILT_DEG, surface_azimuth=ASSUMED_AZIMUTH_DEG,
        solar_zenith=solar_position["apparent_zenith"].to_numpy(),
        solar_azimuth=solar_position["azimuth"].to_numpy(),
        dni=weather_15min["dni_w_m2"].to_numpy(), ghi=weather_15min["ghi_w_m2"].to_numpy(),
        dhi=weather_15min["dhi_w_m2"].to_numpy(), dni_extra=dni_extra.to_numpy(), model="perez",
    )
    poa_global = np.clip(np.asarray(poa["poa_global"]), 0, None)
    cell_temp = pvlib.temperature.faiman(
        poa_global, weather_15min["temperature_c"].to_numpy(), weather_15min["wind_speed_ms"].to_numpy()
    )
    dc_w = pvlib.pvsystem.pvwatts_dc(poa_global, cell_temp, pdc0=1000.0, gamma_pdc=TEMP_COEFF_PDC)  # per 1 kWp
    ac_w = pvlib.inverter.pvwatts(dc_w, pdc0=1000.0)
    ac_kw = np.clip(ac_w, 0, None) / 1000.0 * (1 - WIRING_LOSS)
    return pd.Series(ac_kw, index=times, name="unit_pv_kw")


def compute_clear_midday_mask(weather_15min: pd.DataFrame, lat: float, lon: float) -> pd.Series:
    """Intervals eligible for the k fit: clear sky (actual/clear-sky GHI
    ratio above threshold) and within the midday window (IST)."""
    location = pvlib.location.Location(lat, lon, tz="UTC")
    idx = pd.DatetimeIndex(weather_15min["ts"])
    clearsky_ghi = np.asarray(location.get_clearsky(idx, model="ineichen")["ghi"])
    clear_sky_index = np.where(clearsky_ghi > 10, weather_15min["ghi_w_m2"].to_numpy() / np.maximum(clearsky_ghi, 1e-6), 0.0)
    ist_hour = (idx.hour + idx.minute / 60.0 + IST_OFFSET_HOURS) % 24
    is_midday = (ist_hour >= MIDDAY_HOURS_IST[0]) & (ist_hour < MIDDAY_HOURS_IST[1])
    is_clear = clear_sky_index >= CLEAR_SKY_INDEX_THRESHOLD
    return pd.Series(is_midday & is_clear, index=idx)


def pass0_initial_load_estimate(household_df: pd.DataFrame, net_import: pd.DataFrame) -> pd.DataFrame:
    """A per-sanctioned-kW load shape averaged across non-PV households,
    scaled to each PV household's own sanctioned load."""
    non_pv = household_df[~household_df["has_pv"]]
    pv_hh = household_df[household_df["has_pv"]]

    non_pv_shape = net_import[non_pv["id"]].div(non_pv["sanctioned_load_kw"].to_numpy(), axis=1)
    mean_shape_per_kw = non_pv_shape.mean(axis=1)

    return pd.DataFrame({
        hh_id: mean_shape_per_kw * sanctioned
        for hh_id, sanctioned in zip(pv_hh["id"], pv_hh["sanctioned_load_kw"])
    })


def fit_k(
    load_estimate: pd.DataFrame,
    net_import: pd.DataFrame,
    unit_pv_kw: pd.Series,
    pv_kwp_by_hh: dict[str, float],
    clear_midday_mask: pd.Series,
) -> float:
    """Pass 1 / Pass 3's shared formula: k = sum(load_est - net_import)
    over clear-sky midday intervals, divided by the unit model's total
    modelled output at those same intervals."""
    idx = load_estimate.index[clear_midday_mask.reindex(load_estimate.index, fill_value=False)]
    hh_ids = list(pv_kwp_by_hh.keys())
    numerator = (load_estimate.loc[idx, hh_ids] - net_import.loc[idx, hh_ids]).to_numpy().sum()
    modelled_pv_total = sum(unit_pv_kw.loc[idx].sum() * kwp for kwp in pv_kwp_by_hh.values())
    return float(numerator / modelled_pv_total) if modelled_pv_total > 0 else 0.0


def reconstruct_gross_load(
    household_df: pd.DataFrame, net_import: pd.DataFrame, unit_pv_kw: pd.Series, k: float,
) -> pd.DataFrame:
    """Pass 2: gross = net_import + k * unit_pv_kw * installed_kwp for PV
    households; non-PV households' net import already IS their gross
    load."""
    gross = net_import.copy()
    pv_hh = household_df[household_df["has_pv"]]
    for hh_id, kwp in zip(pv_hh["id"], pv_hh["pv_kwp"]):
        gross[hh_id] = net_import[hh_id] + k * unit_pv_kw.reindex(net_import.index).to_numpy() * kwp
    return gross


if __name__ == "__main__":
    import time

    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.pv_truth import assign_pv_truth_params, generate_true_pv
    from world.weather import fetch_scenario_weather, to_15min

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)

    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    true_pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)

    # Demo-speed simplification, not a correctness shortcut: the four-pass
    # calibration runs on the last 90 days rather than the full year, since
    # Pass 2 retrains gateway/forecast/load_model.py's LightGBM models,
    # whose feature-building loop takes real time per row. The physics and
    # the calibration formulas are identical at any window size.
    window_start = true_load.index.max() - pd.Timedelta(days=90)
    true_load = true_load.loc[true_load.index >= window_start]
    true_pv = true_pv.loc[true_pv.index >= window_start]
    weather_window = weather_15min[weather_15min["ts"] >= window_start].reset_index(drop=True)

    # Net import: what a real meter actually sees. Non-PV households show
    # pure consumption; PV households show consumption minus generation
    # (can go negative = export).
    net_import = true_load.copy()
    pv_hh = feeder.household[feeder.household["has_pv"]]
    for hh_id in pv_hh["id"]:
        if hh_id in true_pv.columns:
            net_import[hh_id] = true_load[hh_id] - true_pv[hh_id].reindex(true_load.index, fill_value=0.0)

    unit_pv_kw = unit_pv_output_kw(lat, lon, weather_window)
    clear_midday_mask = compute_clear_midday_mask(weather_window, lat, lon)
    print(f"clear-sky midday intervals in window: {clear_midday_mask.sum()} / {len(clear_midday_mask)}")

    pv_kwp_by_hh = dict(zip(pv_hh["id"], pv_hh["pv_kwp"]))

    # k_true: the scale factor that WOULD exactly reproduce the simulator's
    # true aggregate PV output using the assumed-parameter unit model —
    # only computable because this is a simulator, never in deployment.
    true_pv_total = true_pv[list(pv_kwp_by_hh.keys())].to_numpy().sum()
    unit_model_total = sum(unit_pv_kw.reindex(true_load.index).sum() * kwp for kwp in pv_kwp_by_hh.values())
    k_true = true_pv_total / unit_model_total if unit_model_total > 0 else 0.0
    print(f"k_true (assumed-parameter unit model vs actual simulator PV): {k_true:.4f}")

    # Pass 0 + Pass 1
    load_estimate_0 = pass0_initial_load_estimate(feeder.household, net_import)
    k1 = fit_k(load_estimate_0, net_import, unit_pv_kw, pv_kwp_by_hh, clear_midday_mask)
    print(f"\nPass 1: k = {k1:.4f}  (k1/k_true = {k1/k_true:.3f})")

    # Pass 2: reconstruct gross load with k1, train the phase-level model.
    t0 = time.perf_counter()
    gross_load = reconstruct_gross_load(feeder.household, net_import, unit_pv_kw, k1)

    import holidays as pyholidays
    years = sorted(gross_load.index.year.unique().tolist())
    holidays_set = {d for d in pyholidays.India(years=years)}
    delay_hours = np.clip(rng.normal(6.0, 2.5, size=len(gross_load)), 0.5, 24.0)

    train_rows = []
    for phase in ["R", "Y", "B"]:
        hh_ids = feeder.household.loc[feeder.household["phase"] == phase, "id"]
        phase_kw = gross_load[hh_ids].sum(axis=1)
        history = pd.DataFrame({
            "ts_end": phase_kw.index, "gross_kw": phase_kw.to_numpy(),
            "received_at": phase_kw.index + pd.to_timedelta(delay_hours, unit="h"),
        })
        phase_hh = feeder.household[feeder.household["phase"] == phase]
        static_features = {
            "household_count": len(phase_hh),
            "total_sanctioned_kw": phase_hh["sanctioned_load_kw"].sum(),
            "ac_cooler_share": ownership.set_index("household_id").loc[phase_hh["id"], ["has_ac", "has_cooler"]].any(axis=1).mean(),
            "commercial_share": phase_hh["is_business"].mean(),
        }
        feats = build_features(
            phase_kw.index, run_time=phase_kw.index.max(), weather_15min=weather_window,
            gross_load_history=history, static_features=static_features,
            holidays_set=holidays_set, festivals_set=set(),
        )
        feats["gross_kw"] = phase_kw.reindex(feats["ts_end"]).to_numpy()
        feats["phase"] = phase
        train_rows.append(feats.dropna(subset=FEATURE_COLUMNS + ["gross_kw"]))

    pass2_train_df = pd.concat(train_rows, ignore_index=True)
    load_model = train_load_model(pass2_train_df)
    print(f"Pass 2: trained on {len(pass2_train_df)} rows in {time.perf_counter()-t0:.1f}s")

    # Pass 3: use the trained model's own P50 predictions (phase-level),
    # apportioned back to PV households by sanctioned load — same style as
    # Pass 0 — as the new load estimate, and refit k.
    predictions = load_model.predict(pass2_train_df)
    predictions["phase"] = pass2_train_df["phase"].to_numpy()
    predictions["ts_end"] = pass2_train_df["ts_end"].to_numpy()

    load_estimate_3 = pd.DataFrame(index=gross_load.index, columns=list(pv_kwp_by_hh.keys()), dtype=float)
    for phase in ["R", "Y", "B"]:
        phase_pred = predictions[predictions["phase"] == phase].set_index("ts_end")["p50_kw"]
        phase_total_sanctioned = feeder.household.loc[feeder.household["phase"] == phase, "sanctioned_load_kw"].sum()
        phase_pv_hh = pv_hh[pv_hh["id"].isin(feeder.household.loc[feeder.household["phase"] == phase, "id"])]
        for hh_id, sanctioned in zip(phase_pv_hh["id"], phase_pv_hh["sanctioned_load_kw"]):
            load_estimate_3[hh_id] = phase_pred.reindex(gross_load.index) * (sanctioned / phase_total_sanctioned)

    k3 = fit_k(load_estimate_3.dropna(), net_import, unit_pv_kw, pv_kwp_by_hh, clear_midday_mask)
    print(f"\nPass 3: k = {k3:.4f}  (k3/k_true = {k3/k_true:.3f})")
    print(f"\nsummary: k_true={k_true:.4f}  k1={k1:.4f} (ratio {k1/k_true:.3f})  "
          f"k3={k3:.4f} (ratio {k3/k_true:.3f})")
