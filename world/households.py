"""world/households.py — appliance-level truth load per household.

Owner A. See Build Specification v1.0 §7.1 and System Architecture v3.0
§11.5 (personas) and §4 (household_truth is hidden from LEO).

Bottom-up appliance model: each household's 15-minute true load is the sum
of a handful of components (a diurnal base load standing in for lighting/
fans/TV/misc, a cycling fridge, temperature-driven AC/cooler duty, a
scheduled pump, and business-hours load), vectorised over
(household x time) with numpy — not a single hand-drawn curve. That's
what makes the evening peak and heat dependence in aggregate load real
(§7.7's realism check): it emerges from many independent appliance
schedules, not from having been drawn that way.

Populates:
  - `appliance` rows (kind, rating_w, count) — fridge for every household,
    ac/cooler/pump if that household happens to own one.
  - `household_truth` rows (persona, has_inverter) — hidden from LEO;
    pv_tilt_deg/pv_azimuth_deg/pv_soiling are world/pv_truth.py's job, not
    this file's, and are left null here.
  - the true load timeseries itself, which is not a DDL table on its own
    — it becomes meter_interval.import_kwh net of PV once world/pv_truth.py
    and sim/measure.py combine it and mask it to what LEO can see.

Feeder.household (world/feeder.py) already carries sanctioned_load_kw,
has_pv, is_business, is_critical — the static registry attributes a
DISCOM would actually hold. AC/cooler/pump/inverter ownership is decided
here instead, since it's truth-side detail no registry carries.
"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd

PERSONAS_RESIDENTIAL = ["price_sensitive", "comfort_first", "already_flexible", "non_responsive"]
PERSONA_BUSINESS = "small_business"

# (verify) Typical Indian household appliance ratings — not a datasheet,
# just plausible order-of-magnitude figures for a peri-urban connection.
APPLIANCE_RATING_KW = {
    "fridge": 0.150,
    "ac": 1.500,
    "cooler": 0.200,
    "pump": 0.750,
}

IST_OFFSET_HOURS = 5.5  # weather/ts is stored UTC; diurnal behaviour is local (§1.5)


def assign_ownership_and_truth(
    household_df: pd.DataFrame, shares: dict, rng: np.random.Generator
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Decide AC/cooler/pump/inverter ownership and hidden persona per
    household. Returns (appliance_rows, household_truth_rows).
    """
    n = len(household_df)
    has_ac = rng.random(n) < shares.get("has_ac", 0.20)
    has_cooler = rng.random(n) < shares.get("has_cooler", 0.35)
    has_pump = rng.random(n) < shares.get("has_pump", 0.15)
    has_inverter = rng.random(n) < shares.get("has_inverter", 0.10)

    appliance_rows = []
    truth_rows = []
    py_rng = random.Random(int(rng.integers(0, 2**31)))

    for i, row in enumerate(household_df.itertuples()):
        appliance_rows.append(
            {"household_id": row.id, "kind": "fridge", "rating_w": APPLIANCE_RATING_KW["fridge"] * 1000, "count": 1}
        )
        if has_ac[i]:
            appliance_rows.append(
                {"household_id": row.id, "kind": "ac", "rating_w": APPLIANCE_RATING_KW["ac"] * 1000, "count": 1}
            )
        if has_cooler[i]:
            appliance_rows.append(
                {"household_id": row.id, "kind": "cooler", "rating_w": APPLIANCE_RATING_KW["cooler"] * 1000, "count": 1}
            )
        if has_pump[i]:
            appliance_rows.append(
                {"household_id": row.id, "kind": "pump", "rating_w": APPLIANCE_RATING_KW["pump"] * 1000, "count": 1}
            )

        persona = PERSONA_BUSINESS if row.is_business else py_rng.choice(PERSONAS_RESIDENTIAL)
        truth_rows.append({
            "household_id": row.id,
            "persona": persona,
            "pv_tilt_deg": None,
            "pv_azimuth_deg": None,
            "pv_soiling": None,
            "has_inverter": bool(has_inverter[i]),
        })

    ownership = pd.DataFrame({"household_id": household_df["id"], "has_ac": has_ac,
                               "has_cooler": has_cooler, "has_pump": has_pump})
    return pd.DataFrame(appliance_rows), pd.DataFrame(truth_rows), ownership


def _bell(x: np.ndarray, center: np.ndarray, width: float) -> np.ndarray:
    return np.exp(-((x - center) ** 2) / (2 * width ** 2))


def generate_true_load(
    household_df: pd.DataFrame,
    ownership: pd.DataFrame,
    weather_15min: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """15-minute true load in kW for every household over the weather
    series' time range. Returns a wide DataFrame indexed by `ts`, one
    column per household id.
    """
    ts = weather_15min["ts"]
    n_t = len(weather_15min)
    n_h = len(household_df)

    hour_ist = (((ts.dt.hour + ts.dt.minute / 60.0) + IST_OFFSET_HOURS) % 24).to_numpy()[None, :]
    temp_c = weather_15min["temperature_c"].to_numpy()[None, :]

    sanctioned_kw = household_df["sanctioned_load_kw"].to_numpy()[:, None]
    is_business = household_df["is_business"].to_numpy()[:, None].astype(float)
    has_ac = ownership["has_ac"].to_numpy()[:, None].astype(float)
    has_cooler = ownership["has_cooler"].to_numpy()[:, None].astype(float)
    has_pump = ownership["has_pump"].to_numpy()[:, None].astype(float)

    # Base load: lighting/fans/TV/misc. Morning and evening bumps, staggered
    # per household so 60 households don't peak in perfect lockstep.
    jitter_h = rng.uniform(-0.4, 0.4, size=(n_h, 1))
    base_scale_kw = sanctioned_kw * rng.uniform(0.10, 0.20, size=(n_h, 1))
    base_kw = base_scale_kw * (
        0.35
        + 0.5 * _bell(hour_ist, 7.5 + jitter_h, 1.5)
        + 1.0 * _bell(hour_ist, 20.0 + jitter_h, 2.0)
    )

    # Fridge: compressor duty-cycles roughly every hour, phase-staggered.
    fridge_duty = 0.35
    fridge_phase_h = rng.uniform(0, 1.0, size=(n_h, 1))
    cycle_pos = ((hour_ist + fridge_phase_h) % 1.0)
    fridge_kw = np.where(cycle_pos < fridge_duty, APPLIANCE_RATING_KW["fridge"], 0.0)

    # AC: duty rises with temperature above a comfort threshold, only when
    # people are typically home/awake to run it.
    ac_duty = np.clip((temp_c - 26.0) / 8.0, 0.0, 1.0)
    ac_hours = ((hour_ist >= 10) & (hour_ist <= 23)).astype(float)
    ac_kw = has_ac * APPLIANCE_RATING_KW["ac"] * ac_duty * ac_hours

    # Cooler: cheaper, lower onset threshold, similar active window.
    cooler_duty = np.clip((temp_c - 24.0) / 10.0, 0.0, 1.0)
    cooler_hours = ((hour_ist >= 9) & (hour_ist <= 22)).astype(float)
    cooler_kw = has_cooler * APPLIANCE_RATING_KW["cooler"] * cooler_duty * cooler_hours

    # Pump: two fixed daily windows, no temperature dependence.
    pump_window = (((hour_ist >= 6) & (hour_ist < 7)) | ((hour_ist >= 18) & (hour_ist < 19))).astype(float)
    pump_kw = has_pump * APPLIANCE_RATING_KW["pump"] * pump_window

    # Business load: extra draw during opening hours.
    business_hours = ((hour_ist >= 9) & (hour_ist <= 20)).astype(float)
    business_kw = is_business * sanctioned_kw * 0.30 * business_hours

    total_kw = base_kw + fridge_kw + ac_kw + cooler_kw + pump_kw + business_kw
    noise = rng.normal(1.0, 0.05, size=(n_h, n_t))
    total_kw = np.clip(total_kw * noise, 0.03, None)  # small standby floor, never negative

    return pd.DataFrame(total_kw.T, index=ts.to_numpy(), columns=household_df["id"].to_numpy()).rename_axis("ts")


if __name__ == "__main__":
    from world.feeder import build_feeder, load_scenario
    from world.weather import fetch_scenario_weather, to_15min

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])

    appliances, truth, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    print(f"appliance rows: {len(appliances)}  (expect >= {len(feeder.household)}, one fridge each plus extras)")
    print(appliances["kind"].value_counts())
    print(f"\npersona mix:\n{truth['persona'].value_counts()}")
    print(f"households with inverter: {truth['has_inverter'].sum()} / {len(truth)}")

    weather_hourly = fetch_scenario_weather(scenario)
    weather_15min = to_15min(weather_hourly)

    load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    print(f"\ntrue load shape: {load.shape} (expect {len(weather_15min)} x {len(feeder.household)})")

    daily_mean = load.mean(axis=1)
    peak_ts = daily_mean.idxmax()
    # 1-4 AM IST == 19:30-22:30 UTC the previous day.
    overnight_ist = daily_mean[daily_mean.index.hour.isin([20, 21, 22])]
    trough_ts = overnight_ist.idxmin()
    print(f"aggregate peak: {daily_mean.max():.3f} kW/household at {peak_ts} UTC")
    print(f"overnight (IST) trough: {daily_mean[trough_ts]:.3f} kW/household at {trough_ts} UTC")
    print(f"total connected sanctioned load: {feeder.household['sanctioned_load_kw'].sum():.1f} kW")
    print(f"peak aggregate demand: {load.sum(axis=1).max():.1f} kW")
