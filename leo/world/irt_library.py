"""world/irt_library.py — perfect-foresight trajectory library.

Owner A. See Build Specification v1.0 §5.3 and System Architecture v3.0
§9.1.

For each historical day and each phase, solves a small convex program
using TRUE (perfect-foresight) load and PV for that day plus a price
signal, giving the SoC trajectory the battery WOULD have followed had it
known everything in advance. This is the "cold start" for the IRT
reference: orchestrator/irt.py (not built yet) finds days similar to
tomorrow's forecast weather/calendar and blends the top-10 trajectories
from this library; in deployment the library is progressively replaced by
realised operating history. "Household load is never available in time to
use this way" (§9.1) is exactly why this library is built from a day's
OWN true load/PV rather than anything the live planner could observe.

Deliberately NOT network-constrained. Incorporating the real per-interval
safe charge/discharge limits (gateway/network_model.py) would need a
network solve per interval per day per phase — 365 x 96 x 3 solves, tens
of minutes at best for one build. The library's job is a target SHAPE for
the live planner's deviation penalty, not a final approved dispatch; the
live planner (orchestrator/planner_cvx.py, not built yet) is what applies
the real network limits and reserve floor.

Two prices, matching v3.0 §9.1: cost of charging from the DISCOM ToD
tariff, value of discharging from the IEX real-time price.
cloud/connectors.py (Owner B, not built) is the eventual real source for
both; this file uses documented placeholder curves shaped like the real
thing until that lands. (verify)

Discharge value is weighted by that day's own net-load shape (load minus
PV, min-max normalised) so the optimiser is rewarded more for discharging
into that day's actual stress hours than into an arbitrary evening
window — the closest defensible proxy for "value in violation windows"
(§5.3) without a network solve in the loop.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import cvxpy as cp

MODULE_DIR = Path(__file__).parent

# (verify) Karnataka ToD tariff structure and rates are assumed, not
# confirmed against a current tariff order (System Architecture v3.0 §9.3
# flags this explicitly). A representative 3-band peak/normal/off-peak
# shape, common across Indian ToD tariffs.
TOD_RATES_PAISE_PER_KWH = {"peak": 900.0, "normal": 700.0, "off_peak": 450.0}
IST_OFFSET_HOURS = 5.5


def _ist_hour(ts: pd.DatetimeIndex) -> np.ndarray:
    # DatetimeIndex.hour/.minute return an Index, not an ndarray — force
    # conversion so downstream arithmetic can't silently leak pandas
    # objects into a cvxpy Parameter assignment.
    return (ts.hour.to_numpy() + ts.minute.to_numpy() / 60.0 + IST_OFFSET_HOURS) % 24


def tod_charge_price_paise(ts: pd.DatetimeIndex) -> np.ndarray:
    ist_hour = _ist_hour(ts)
    price = np.full(len(ts), TOD_RATES_PAISE_PER_KWH["normal"])
    price = np.where((ist_hour >= 18) & (ist_hour < 22), TOD_RATES_PAISE_PER_KWH["peak"], price)
    price = np.where((ist_hour >= 23) | (ist_hour < 6), TOD_RATES_PAISE_PER_KWH["off_peak"], price)
    return price.astype(float)


def iex_proxy_value_paise(
    ts: pd.DatetimeIndex, temperature_c: np.ndarray, rng: np.random.Generator
) -> np.ndarray:
    """A stylised IEX real-time price stand-in: bimodal daily shape
    (morning + evening demand peaks), day-to-day log-normal volatility,
    and a level that rises with that day's mean temperature. Real IEX
    real-time prices do track system-wide temperature-driven demand —
    Karnataka's peak load is heavily AC-driven statewide, so a hot day
    pushes spot prices up across the whole grid, not just locally. Without
    this term the price signal has ~zero correlation with weather (an
    earlier version of this function did — confirmed via a leave-one-out
    validation in orchestrator/irt.py showing the similarity blend
    couldn't beat a flat all-day average, because there was no
    weather-linked variation for it to exploit). Not fetched from IEX —
    cloud/connectors.py's job once built. (verify)
    """
    ist_hour = _ist_hour(ts)
    shape = (
        300.0
        + 250.0 * np.exp(-((ist_hour - 10.0) ** 2) / (2 * 1.5 ** 2))
        + 400.0 * np.exp(-((ist_hour - 19.5) ** 2) / (2 * 1.5 ** 2))
    )
    day = pd.Series(ts.date)
    day_factor = {d: rng.lognormal(mean=0.0, sigma=0.25) for d in day.unique()}
    noise_factor = day.map(day_factor).to_numpy()

    daily_mean_temp = pd.Series(temperature_c).groupby(day).transform("mean").to_numpy()
    temp_effect = 1.0 + np.clip((daily_mean_temp - 22.0) / 10.0, -0.3, 0.6)

    return shape * noise_factor * temp_effect


class DaySolver:
    """A single compiled cvxpy problem, re-solved once per (day, phase)
    with updated parameters. Compiling once and reusing via warm-started
    re-solves is what keeps ~1,000 small QPs (365 days x 3 phases)
    tractable — recompiling from scratch each time dominates the runtime
    of a problem this small.
    """

    def __init__(
        self, capacity_kwh: float, power_kw: float, soc_min: float, soc_max: float,
        efficiency: float, charge_price_paise: np.ndarray,
        initial_soc_frac: float = 0.5, interval_hours: float = 0.25,
    ):
        n = len(charge_price_paise)
        self.capacity_kwh = capacity_kwh
        self.charge_kw = cp.Variable(n, nonneg=True)
        self.discharge_kw = cp.Variable(n, nonneg=True)
        self.soc_kwh = cp.Variable(n + 1)
        self.effective_value_paise = cp.Parameter(n, nonneg=True)

        cost = cp.sum(cp.multiply(self.charge_kw, charge_price_paise)) * interval_hours
        revenue = cp.sum(cp.multiply(self.discharge_kw, self.effective_value_paise)) * interval_hours

        constraints = [
            self.charge_kw <= power_kw,
            self.discharge_kw <= power_kw,
            self.soc_kwh[0] == initial_soc_frac * capacity_kwh,
            self.soc_kwh[n] == self.soc_kwh[0],  # daily cycle: no free energy across day boundaries
            self.soc_kwh >= soc_min * capacity_kwh,
            self.soc_kwh <= soc_max * capacity_kwh,
        ]
        for t in range(n):
            constraints.append(
                self.soc_kwh[t + 1]
                == self.soc_kwh[t] + self.charge_kw[t] * efficiency * interval_hours
                - self.discharge_kw[t] * interval_hours
            )
        self.problem = cp.Problem(cp.Minimize(cost - revenue), constraints)

    def solve(self, net_load_kw: np.ndarray, discharge_value_paise: np.ndarray) -> np.ndarray:
        lo, hi = net_load_kw.min(), net_load_kw.max()
        stress_weight = (net_load_kw - lo) / (hi - lo + 1e-9)
        self.effective_value_paise.value = discharge_value_paise * stress_weight

        # This file's objective (cost - revenue) is purely linear — there's
        # no quadratic IRT-deviation term here, since building the IRT
        # library is what avoids that circularity. OSQP is an ADMM QP
        # solver and stalls/oscillates on genuinely linear, degenerate
        # problems like this one (confirmed: it hit max_iter without
        # converging). HIGHS is an actual LP solver and is the right tool
        # for this file's formulation; planner_cvx.py's real objective
        # does have a quadratic term and uses OSQP as the Build Spec
        # specifies (§5.3).
        self.problem.solve(solver=cp.HIGHS)
        if self.problem.status not in ("optimal", "optimal_inaccurate"):
            raise RuntimeError(f"IRT day solve failed: {self.problem.status}")
        return self.soc_kwh.value[1:] / self.capacity_kwh


def build_library(
    household_df: pd.DataFrame,
    true_load_kw: pd.DataFrame,
    true_pv_kw: pd.DataFrame,
    battery_blocks: list[dict],
    rng: np.random.Generator,
    temperature_c: np.ndarray,
) -> pd.DataFrame:
    """One perfect-foresight SoC trajectory per (day, phase). Returns a
    long DataFrame: date, phase, interval (0-95), soc_frac.

    `temperature_c` must align with true_load_kw.index — it's what lets
    the discharge-value proxy track that day's heat (see
    iex_proxy_value_paise), which is what gives orchestrator/irt.py's
    similarity blend something weather-linked to actually exploit.
    """
    ts_all = true_load_kw.index
    charge_price = tod_charge_price_paise(ts_all)
    discharge_value = iex_proxy_value_paise(ts_all, temperature_c, rng)

    dates = pd.Series(ts_all.date)
    unique_dates = sorted(dates.unique())

    pv_by_hh = true_pv_kw.reindex(columns=household_df["id"], fill_value=0.0)

    rows = []
    for block in battery_blocks:
        phase = block["phase"]
        hh_ids = household_df.loc[household_df["phase"] == phase, "id"]
        phase_load = true_load_kw[hh_ids].sum(axis=1)
        phase_pv = pv_by_hh[hh_ids].sum(axis=1)
        net_load = (phase_load - phase_pv).to_numpy()

        day_mask_cache = {d: (dates.to_numpy() == d) for d in unique_dates}
        solver = None

        for d in unique_dates:
            mask = day_mask_cache[d]
            if mask.sum() != 96:
                continue  # partial day at the edge of the series, skip
            if solver is None:
                solver = DaySolver(
                    capacity_kwh=block["capacity_kwh"], power_kw=block["power_kw"],
                    soc_min=0.15, soc_max=0.90, efficiency=0.92,
                    charge_price_paise=charge_price[mask],
                )
            traj = solver.solve(net_load[mask], discharge_value[mask])
            for i, soc_frac in enumerate(traj):
                rows.append({"date": d, "phase": phase, "interval": i, "soc_frac": soc_frac})

    return pd.DataFrame(rows)


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
    load = generate_true_load(feeder.household, ownership, weather_15min, rng)

    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)

    t0 = time.perf_counter()
    library = build_library(
        feeder.household, load, pv, scenario["battery_blocks"], rng,
        temperature_c=weather_15min["temperature_c"].to_numpy(),
    )
    print(f"solved {library['date'].nunique() * library['phase'].nunique()} day-phase trajectories "
          f"in {time.perf_counter()-t0:.1f}s")
    print(f"library shape: {library.shape}")

    for phase in ["R", "Y", "B"]:
        phase_lib = library[library.phase == phase]
        by_interval = phase_lib.groupby("interval")["soc_frac"].mean()
        charge_hours = by_interval.diff().gt(0).sum()
        print(f"phase {phase}: mean SoC ranges {by_interval.min():.2f}-{by_interval.max():.2f}, "
              f"peak SoC at interval {by_interval.idxmax()} (~{by_interval.idxmax()*15//60:.0f}:{by_interval.idxmax()*15%60:02.0f}), "
              f"trough at interval {by_interval.idxmin()} (~{by_interval.idxmin()*15//60:.0f}:{by_interval.idxmin()*15%60:02.0f})")

    out_dir = MODULE_DIR / "data" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    library.to_parquet(out_dir / "irt_library.parquet", index=False)
    print(f"wrote {out_dir / 'irt_library.parquet'}")
