"""world/pv_truth.py — true rooftop PV output with hidden true parameters.

Owner A. See Build Specification v1.0 §5.2 (physics chain) and §7.1 (the
simulator knows the truth; LEO never does).

This is the SIMULATOR's ground truth: a real tilt, azimuth and soiling
factor per PV household, hidden in `household_truth` (which the
forecaster must never read), run through pvlib's physics chain against
the real cached irradiance from world/weather.py.

This is deliberately NOT the same model as gateway/forecast/pv_model.py.
That one calibrates a scale factor `k` because the forecaster never
observes these hidden parameters or true output behind a net meter; this
one doesn't need to, because it IS the source of the truth being
calibrated against.

Chain: solar position -> POA irradiance (Perez, using the real GHI/DNI/DHI
world/weather.py already fetched, so no Erbs decomposition is needed) ->
cell temperature (Faiman) -> DC power (PVWatts) -> AC power (PVWatts
inverter model, with clipping) -> soiling and wiring losses.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

# (verify) §5.2's assumed installation parameters — a rooftop system in
# this segment is rarely installed with instrumented tilt/azimuth, so
# these are typical, plausible values, not a datasheet.
TILT_RANGE_DEG = (10.0, 15.0)
AZIMUTH_MEAN_DEG = 180.0   # due south
AZIMUTH_STD_DEG = 15.0     # installer variation
SOILING_RANGE = (0.92, 0.98)  # fraction of irradiance retained

WIRING_LOSS = 0.02
TEMP_COEFF_PDC = -0.0037  # %/degC, typical crystalline-silicon module


def assign_pv_truth_params(household_df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Hidden true tilt/azimuth/soiling for every has_pv household. Not
    merged into household_truth here — world/build.py (the entry point
    that runs every generator) owns combining households.py's truth rows
    with this file's, since either could in principle run first.
    """
    pv_hh = household_df[household_df["has_pv"]]
    n = len(pv_hh)
    return pd.DataFrame({
        "household_id": pv_hh["id"].to_numpy(),
        "pv_tilt_deg": rng.uniform(*TILT_RANGE_DEG, size=n),
        "pv_azimuth_deg": rng.normal(AZIMUTH_MEAN_DEG, AZIMUTH_STD_DEG, size=n),
        "pv_soiling": rng.uniform(*SOILING_RANGE, size=n),
    })


def true_ac_output_kw(
    lat: float, lon: float, tilt_deg: float, azimuth_deg: float, soiling: float, kwp: float,
    weather_15min: pd.DataFrame,
    solar_position: pd.DataFrame | None = None,
    dni_extra: pd.Series | None = None,
) -> np.ndarray:
    """One household's true AC output (kW) at the weather series'
    resolution (15-min, once fed through world.weather.to_15min).

    `solar_position` and `dni_extra` depend only on (lat, lon, time), not
    on the household, so generate_true_pv() computes them once and passes
    them in rather than recomputing per household.
    """
    times = pd.DatetimeIndex(weather_15min["ts"])
    if solar_position is None:
        solar_position = pvlib.solarposition.get_solarposition(times, lat, lon)
    if dni_extra is None:
        dni_extra = pvlib.irradiance.get_extra_radiation(times)

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt_deg, surface_azimuth=azimuth_deg,
        solar_zenith=solar_position["apparent_zenith"].to_numpy(),
        solar_azimuth=solar_position["azimuth"].to_numpy(),
        dni=weather_15min["dni_w_m2"].to_numpy(),
        ghi=weather_15min["ghi_w_m2"].to_numpy(),
        dhi=weather_15min["dhi_w_m2"].to_numpy(),
        dni_extra=dni_extra.to_numpy(),
        model="perez",
    )
    poa_global = np.clip(np.asarray(poa["poa_global"]), 0, None)

    cell_temp = pvlib.temperature.faiman(
        poa_global, weather_15min["temperature_c"].to_numpy(), weather_15min["wind_speed_ms"].to_numpy(),
    )

    pdc0_w = kwp * 1000.0
    dc_w = pvlib.pvsystem.pvwatts_dc(poa_global, cell_temp, pdc0=pdc0_w, gamma_pdc=TEMP_COEFF_PDC)
    ac_w = pvlib.inverter.pvwatts(dc_w, pdc0=pdc0_w)  # eta_inv_nom default handles efficiency + clipping

    ac_kw = np.clip(ac_w, 0, None) / 1000.0
    ac_kw *= soiling * (1 - WIRING_LOSS)
    return ac_kw


def generate_true_pv(
    household_df: pd.DataFrame, pv_truth: pd.DataFrame, lat: float, lon: float,
    weather_15min: pd.DataFrame,
) -> pd.DataFrame:
    """True AC output (kW) for every PV household, wide DataFrame indexed
    by `ts` like world/households.py's generate_true_load(), so the two
    combine directly into gross load (§10.2: gross = import - export +
    estimated_PV, though here both sides of that equation are truth, not
    estimates).
    """
    times = pd.DatetimeIndex(weather_15min["ts"])
    solar_position = pvlib.solarposition.get_solarposition(times, lat, lon)  # shared across all households
    dni_extra = pvlib.irradiance.get_extra_radiation(times)

    kwp_by_id = dict(zip(household_df["id"], household_df["pv_kwp"]))
    columns = {}
    for row in pv_truth.itertuples():
        columns[row.household_id] = true_ac_output_kw(
            lat, lon, row.pv_tilt_deg, row.pv_azimuth_deg, row.pv_soiling,
            kwp_by_id[row.household_id], weather_15min, solar_position, dni_extra,
        )
    return pd.DataFrame(columns, index=times).rename_axis("ts")


if __name__ == "__main__":
    from world.feeder import build_feeder, load_scenario
    from world.weather import fetch_scenario_weather, to_15min

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])

    pv_truth = assign_pv_truth_params(feeder.household, rng)
    print(f"PV households: {len(pv_truth)} / {len(feeder.household)}")
    print(pv_truth.describe()[["pv_tilt_deg", "pv_azimuth_deg", "pv_soiling"]])

    weather_15min = to_15min(fetch_scenario_weather(scenario))
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]

    pv_output = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)
    print(f"\ntrue PV output shape: {pv_output.shape}")

    total_kwp = feeder.household.loc[feeder.household["has_pv"], "pv_kwp"].sum()
    aggregate = pv_output.sum(axis=1)
    print(f"total installed PV: {total_kwp:.2f} kWp")
    print(f"peak aggregate AC output: {aggregate.max():.2f} kW "
          f"({aggregate.max()/total_kwp*100:.0f}% of installed kWp — near-equatorial tilt/soiling "
          f"losses are small, and POA irradiance can exceed the 1000 W/m^2 STC reference briefly)")

    midnight = aggregate[aggregate.index.hour == 20]  # ~1:30 AM IST
    print(f"night output (expect ~0): max={midnight.max():.4f} kW")

    solar_noon = aggregate[(aggregate.index.hour == 6) & (aggregate.index.minute == 30)]
    print(f"~solar noon output on an average day: {solar_noon.mean():.2f} kW")

    daily_peak = aggregate.resample("D").max()
    daily_max_temp = pd.Series(
        fetch_scenario_weather(scenario).set_index("ts")["temperature_c"]
    ).resample("D").max()
    common = daily_peak.index.intersection(daily_max_temp.index)
    corr = np.corrcoef(daily_peak.loc[common], daily_max_temp.loc[common])[0, 1]
    print(f"correlation(daily peak PV output, daily max temp) = {corr:.3f} "
          f"(two opposing effects: hot pre-monsoon days tend to be clear -> more sun, "
          f"but panels also derate as they get hotter -> less output per watt of sun)")
