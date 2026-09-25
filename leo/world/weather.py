"""world/weather.py — pulls and caches real weather and irradiance.

Owner A. See Build Specification v1.0 §9.1 (dataset), §10.2 (load-model
features) and §5.2 (PV model irradiance chain).

Two Open-Meteo endpoints, both real, both free, no key required:
  - archive-api.open-meteo.com/v1/archive  — historical hourly, any past
    date range. Feeds the one-year truth/training period.
  - api.open-meteo.com/v1/forecast          — current + up to 16-day
    forecast, plus a `past_days` window. Feeds the day-ahead (14:00) and
    morning re-run (06:00) forecast jobs.

Both are cached to Parquet under world/data/weather/ so a full year isn't
re-fetched on every `world.build` run.

Hourly variables pulled: temperature, relative humidity, precipitation,
wind speed, and all three irradiance components (GHI, DNI, DHI) plus
cloud cover. Open-Meteo provides DNI and DHI directly, so pv_truth.py
doesn't need the Erbs GHI-decomposition fallback the architecture doc
allows for — that fallback only matters if a source gives GHI alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import requests

MODULE_DIR = Path(__file__).parent
DEFAULT_CACHE_DIR = MODULE_DIR / "data" / "weather"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "shortwave_radiation",       # GHI, W/m^2
    "direct_normal_irradiance",  # DNI, W/m^2
    "diffuse_radiation",         # DHI, W/m^2
    "cloud_cover",
]

_RENAME = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "wind_speed_10m": "wind_speed_ms",
    "shortwave_radiation": "ghi_w_m2",
    "direct_normal_irradiance": "dni_w_m2",
    "diffuse_radiation": "dhi_w_m2",
    "cloud_cover": "cloud_cover_pct",
}


def _parse_hourly_response(payload: dict) -> pd.DataFrame:
    df = pd.DataFrame(payload["hourly"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.rename(columns={"time": "ts", **_RENAME})
    return df


def fetch_historical(
    lat: float, lon: float, start_date: str, end_date: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> pd.DataFrame:
    """Hourly historical weather + irradiance for [start_date, end_date]
    (inclusive, 'YYYY-MM-DD'), cached to Parquet keyed by location and
    range so repeat calls don't re-hit the API.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"historical_{lat:.4f}_{lon:.4f}_{start_date}_{end_date}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat, "longitude": lon,
            "start_date": start_date, "end_date": end_date,
            "hourly": ",".join(HOURLY_VARIABLES),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        },
        timeout=60,
    )
    resp.raise_for_status()
    df = _parse_hourly_response(resp.json())
    df.to_parquet(cache_path, index=False)
    return df


def fetch_forecast(
    lat: float, lon: float, forecast_days: int = 2, past_days: int = 0,
) -> pd.DataFrame:
    """Current hourly forecast for the day-ahead (14:00) and morning
    (06:00) runs. Not cached — a fresh pull is the point of a forecast."""
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat, "longitude": lon,
            "forecast_days": forecast_days, "past_days": past_days,
            "hourly": ",".join(HOURLY_VARIABLES),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return _parse_hourly_response(resp.json())


def to_15min(hourly: pd.DataFrame) -> pd.DataFrame:
    """Interpolate hourly weather to 15-minute intervals (LEO's native
    resolution everywhere except live sensor/battery data, §1.5).

    Linear interpolation is an acceptable stand-in for the smooth fields
    (temperature, humidity, wind); irradiance ideally wants a clear-sky-
    aware disaggregation rather than a straight line between hourly means,
    since GHI can swing sharply intra-hour under broken cloud. (verify —
    flagged as an approximation until pv_truth.py's clear-sky model is
    available to refine it.)
    """
    indexed = hourly.set_index("ts").sort_index()
    last_ts = indexed.index[-1] + pd.Timedelta(minutes=45)
    reindexed = indexed.reindex(
        pd.date_range(indexed.index[0], last_ts, freq="15min")
    )
    interpolated = reindexed.interpolate(method="linear").ffill().bfill()
    return interpolated.rename_axis("ts").reset_index()


def fetch_scenario_weather(scenario: dict, cache_dir: Path = DEFAULT_CACHE_DIR) -> pd.DataFrame:
    """Convenience wrapper: pulls the historical hourly series covering
    the scenario's simulated date range, at the scenario's neighbourhood
    centroid."""
    nb = scenario["neighbourhood"]
    sim = scenario["sim"]
    return fetch_historical(nb["centroid_lat"], nb["centroid_lon"], sim["date_start"], sim["date_end"], cache_dir)


if __name__ == "__main__":
    from world.feeder import load_scenario

    scenario = load_scenario()
    hourly = fetch_scenario_weather(scenario)
    print(f"fetched {len(hourly)} hourly rows, "
          f"{hourly['ts'].min()} .. {hourly['ts'].max()}")
    print(hourly[["ts", "temperature_c", "humidity_pct", "ghi_w_m2", "dni_w_m2", "dhi_w_m2"]].head())

    quarter_hourly = to_15min(hourly)
    print(f"\ninterpolated to {len(quarter_hourly)} 15-min rows")
    print(f"nulls after interpolation: {int(quarter_hourly.isna().sum().sum())}")

    # IST is UTC+5:30, so local solar noon (~12:00 IST) falls near 06:30 UTC.
    solar_noon = quarter_hourly[
        (quarter_hourly["ts"].dt.hour == 6) & (quarter_hourly["ts"].dt.minute == 30)
    ].iloc[0]
    midnight_utc = quarter_hourly[quarter_hourly["ts"].dt.hour == 0].iloc[0]
    print(f"\nsanity: ~solar noon GHI={solar_noon['ghi_w_m2']:.0f} W/m^2, "
          f"midnight GHI={midnight_utc['ghi_w_m2']:.0f} W/m^2 (expect ~0 at midnight, high near solar noon)")
