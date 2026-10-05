"""gateway/orchestrator/planner_shave.py — peak-shaving battery dispatch.

Replaces the "discharge flat out the moment the far end dips" behaviour,
which emptied every block by ~19:00 on the peak day — before the worst
interval and before the DR window. The battery's job in the evening is to
take the TOP off the phase's load curve, so the energy it holds is spent
where it buys the most: the highest-load intervals.

Both directions are water-filling problems on the phase's load curve:

  evening  find the level T such that shaving everything above T uses
           exactly the energy the block holds; discharge = load - T.
  midday   find the level C such that filling everything below C uses
           exactly the energy the block can take; charge = C - load.
           The lowest-load midday intervals are the ones where rooftop PV
           is exporting, so the block absorbs local surplus first.

The same functions run day-ahead on the forecast (the plan the operator
approves) and live every 15 minutes on what is left of the window, with
the phase load measured by a CT on the busbar sensor (Architecture §C1:
a current clamp added to each busbar voltage sensor). Re-solving on the
remaining window is what keeps a forecast error from stranding energy or
running the block empty before the peak.
"""

from __future__ import annotations

import numpy as np

INTERVAL_H = 0.25
CHARGE_WINDOW_IST = (8.5, 16.5)      # solar hours: charge from midday surplus
DISCHARGE_WINDOW_IST = (16.5, 23.5)  # evening ramp and peak


def _fill_level(load: np.ndarray, energy_kwh: float, power_kw: float, above: bool) -> float:
    """Bisection for the water level. above=True: shave load above the
    level; above=False: fill load below it. Each interval moves at most
    `power_kw`."""
    if energy_kwh <= 0 or len(load) == 0:
        return float(np.max(load)) + 1.0 if above and len(load) else float(np.min(load)) - 1.0 if len(load) else 0.0
    lo, hi = float(np.min(load)) - power_kw, float(np.max(load)) + power_kw
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        moved = np.clip(load - mid if above else mid - load, 0.0, power_kw).sum() * INTERVAL_H
        if above:
            lo, hi = (mid, hi) if moved > energy_kwh else (lo, mid)
        else:
            lo, hi = (lo, mid) if moved > energy_kwh else (mid, hi)
    return 0.5 * (lo + hi)


def shave_setpoints(load_kw: np.ndarray, energy_kwh: float, power_kw: float, floor_kw: float = 0.0) -> np.ndarray:
    """Discharge kW per interval that flattens `load_kw` from the top using
    at most `energy_kwh` (delivered, AC side). Never below `floor_kw`: the
    block does not push power back up through the transformer."""
    level = max(_fill_level(load_kw, energy_kwh, power_kw, above=True), floor_kw)
    return np.clip(load_kw - level, 0.0, power_kw)


def fill_setpoints(load_kw: np.ndarray, energy_kwh: float, power_kw: float) -> np.ndarray:
    """Charge kW (positive magnitude) per interval that fills `load_kw` from
    the bottom using `energy_kwh` (drawn, AC side)."""
    level = _fill_level(load_kw, energy_kwh, power_kw, above=False)
    return np.clip(level - load_kw, 0.0, power_kw)


def in_window(local_hour: np.ndarray | float, window: tuple[float, float]):
    return (local_hour >= window[0]) & (local_hour < window[1])


def plan_day(
    forecast_kw: np.ndarray,
    local_hour: np.ndarray,
    capacity_kwh: float,
    power_kw: float,
    soc0: float,
    soc_min: float,
    soc_max: float,
    efficiency: float,
) -> np.ndarray:
    """Day-ahead plan for one phase: 96 setpoints, + discharge / - charge,
    the sign convention used everywhere in LEO. Assumes the block is
    refilled to soc_max at midday and spent down to soc_min by night."""
    setpoints = np.zeros(len(forecast_kw))
    ch = in_window(local_hour, CHARGE_WINDOW_IST)
    dis = in_window(local_hour, DISCHARGE_WINDOW_IST)
    charge_kwh = max(0.0, (soc_max - soc0) * capacity_kwh / efficiency)
    setpoints[ch] = -fill_setpoints(forecast_kw[ch], charge_kwh, power_kw)
    deliverable_kwh = (soc_max - soc_min) * capacity_kwh * efficiency
    setpoints[dis] = shave_setpoints(forecast_kw[dis], deliverable_kwh, power_kw)
    return setpoints


def live_setpoint(
    i: int,
    measured_kw: float,
    forecast_kw: np.ndarray,
    forecast_bias_kw: float,
    local_hour: np.ndarray,
    soc: float,
    capacity_kwh: float,
    power_kw: float,
    soc_min: float,
    soc_max: float,
    efficiency: float,
) -> tuple[float, str | None]:
    """Receding-horizon re-solve at interval i. The current interval uses
    the measured load; the rest of the window uses the forecast shifted by
    the recent forecast error. Returns (setpoint_kw, reason)."""
    h = local_hour[i]
    if in_window(h, DISCHARGE_WINDOW_IST):
        rest = np.where(in_window(local_hour[i:], DISCHARGE_WINDOW_IST))[0] + i
        curve = forecast_kw[rest] + forecast_bias_kw
        curve[0] = measured_kw
        energy = max(0.0, (soc - soc_min) * capacity_kwh * efficiency)
        sp = float(shave_setpoints(curve, energy, power_kw)[0])
        return min(sp, max(0.0, measured_kw)), "peak_shave" if sp > 0 else None
    if in_window(h, CHARGE_WINDOW_IST):
        rest = np.where(in_window(local_hour[i:], CHARGE_WINDOW_IST))[0] + i
        curve = forecast_kw[rest] + forecast_bias_kw
        curve[0] = measured_kw
        energy = max(0.0, (soc_max - soc) * capacity_kwh / efficiency)
        sp = float(fill_setpoints(curve, energy, power_kw)[0])
        return -sp, ("absorb_surplus" if measured_kw < 0 else "valley_fill") if sp > 0 else None
    return 0.0, None
