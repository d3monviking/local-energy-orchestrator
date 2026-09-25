"""gateway/orchestrator/planner_cvx.py — convex day-ahead battery plan.

Owner A. See Build Specification v1.0 §5.3 and System Architecture v3.0
§9.1.

Same interface as planner_rules.py (a list of IntervalForecast in, a
96-interval plan out) — the orchestrator picks between them by config,
per §5.3's "build the fallback first; both implement the same interface."

Objective: minimise ToD charging cost, minus flexibility value `v` for
discharge specifically in forecast violation intervals, plus a quadratic
penalty on deviation from the IRT reference trajectory
(orchestrator/irt.py). Unlike irt_library.py's bootstrap solve (a pure
LP, §5.3's "cold start"), the IRT deviation term makes this a genuine QP
— which is why OSQP, built for QPs, is the right solver here (irt_library
needed HIGHS instead, precisely because it lacks this term).

Constraints: SoC dynamics with round-trip efficiency, SoC bounds (already
reflecting any second-life derating via soc_min/soc_max), the REAL
per-interval per-phase safe charge/discharge limits from
gateway/network_model.py (not just the battery's own rating), and the
reserve floor held throughout every interval, not just at day's end.
"""

from __future__ import annotations

import cvxpy as cp
import numpy as np

from gateway.orchestrator.planner_rules import IntervalForecast

# (verify/tune) Weight on (SoC fraction - IRT reference)^2 per interval.
# Empirically steep: on a synthetic evening-violation test day, weight=0
# (pure economics) served 8.75 kWh into real violation intervals; even
# weight=0.1 cut that to 7.05 kWh for only a small RMSE-to-reference
# improvement (0.232 -> 0.222), and weight=1 cut it to 4.51 kWh. The
# reference trajectory is blended from OTHER similar-weather days, which
# had their own violation timing — chasing its exact shape can trade off
# against responding to tonight's actual conditions. This is expected,
# not a bug: v3.0 §9.1 calls the IRT-vs-perfect-foresight gap "a standing
# optimality diagnostic" to monitor in production, not something to
# eliminate at prototype stage. Kept small so the term acts as a mild
# prior/tie-breaker rather than overriding real violation response.
IRT_DEVIATION_WEIGHT = 0.1

# A tiny quadratic regulariser on charge/discharge power, independent of
# IRT_DEVIATION_WEIGHT. Without it, setting the IRT weight to 0 (or very
# small) leaves a purely linear objective — the same degenerate-LP
# problem world/irt_library.py hit with OSQP (confirmed: this file's
# solve raised "user_limit" at weight=0 before this was added). This
# keeps the problem a genuine QP regardless of how the IRT weight is
# tuned, so solver stability and "how much do we care about the IRT
# shape" are two separate knobs, not one conflated one.
POWER_REGULARIZATION = 1e-3

# (verify/tune) §5.3: "value at v for discharge in violation windows... IEX
# real-time price... scaled by forecast severity." Without this, v is just
# a generic wholesale price with no link to how bad the local violation
# is — confirmed this mattered: with an unscaled v, a routine market price
# came in below the ToD charging cost for a real 11-interval violation
# window, so the optimizer rationally refused to pre-charge for it and
# only spent energy already on hand. severity is in pu-beyond-limit units
# (typically a few percent for a real violation); this scale makes even a
# modest excursion raise v several-fold, reflecting that avoiding an
# actual grid violation is worth materially more than routine arbitrage.
SEVERITY_VALUE_SCALE = 100.0


def plan_convex(
    capacity_kwh: float,
    power_kw: float,
    soc_min: float,
    soc_max: float,
    efficiency: float,
    intervals: list[IntervalForecast],
    reserve_floor_kwh: float,
    initial_soc_frac: float,
    charge_price_paise: np.ndarray,
    discharge_value_paise: np.ndarray,
    irt_reference_soc_frac: np.ndarray,
    irt_deviation_weight: float = IRT_DEVIATION_WEIGHT,
    interval_hours: float = 0.25,
) -> list[dict]:
    """Solve one phase's day-ahead plan. Returns a list of dicts matching
    the `intervals` items in contracts/schemas/plan.schema.json, same
    shape as planner_rules.plan_rule_based()'s output.
    """
    n = len(intervals)
    max_charge_kw = np.array([iv.max_charge_kw for iv in intervals])
    max_discharge_kw = np.array([iv.max_discharge_kw for iv in intervals])
    severity = np.array([iv.violation_severity for iv in intervals])
    is_violation = (severity > 0).astype(float)

    # §5.3: value is the IEX-proxy price scaled by forecast severity, not
    # the raw price — see SEVERITY_VALUE_SCALE's note on why this matters.
    effective_value_paise = discharge_value_paise * (1.0 + SEVERITY_VALUE_SCALE * severity)

    charge_kw = cp.Variable(n, nonneg=True)
    discharge_kw = cp.Variable(n, nonneg=True)
    soc_kwh = cp.Variable(n + 1)

    cost = cp.sum(cp.multiply(charge_kw, charge_price_paise)) * interval_hours
    # Flexibility value is earned only in forecast violation intervals —
    # §5.3's "value at v for discharge in violation windows", now that a
    # real per-interval violation flag is available (unlike the offline
    # IRT library, which has no network model in its loop).
    revenue = cp.sum(
        cp.multiply(cp.multiply(discharge_kw, is_violation), effective_value_paise)
    ) * interval_hours
    soc_frac = soc_kwh[1:] / capacity_kwh
    deviation_penalty = irt_deviation_weight * cp.sum_squares(soc_frac - irt_reference_soc_frac)
    regularizer = POWER_REGULARIZATION * (cp.sum_squares(charge_kw) + cp.sum_squares(discharge_kw))

    objective = cp.Minimize(cost - revenue + deviation_penalty + regularizer)

    constraints = [
        charge_kw <= max_charge_kw, discharge_kw <= max_discharge_kw,
        charge_kw <= power_kw, discharge_kw <= power_kw,
        soc_kwh[0] == initial_soc_frac * capacity_kwh,
        soc_kwh >= reserve_floor_kwh,  # held every interval, not just at day's end
        soc_kwh >= soc_min * capacity_kwh,
        soc_kwh <= soc_max * capacity_kwh,
    ]
    for t in range(n):
        constraints.append(
            soc_kwh[t + 1]
            == soc_kwh[t] + charge_kw[t] * efficiency * interval_hours - discharge_kw[t] * interval_hours
        )

    problem = cp.Problem(objective, constraints)
    problem.solve(solver=cp.OSQP)
    if problem.status not in ("optimal", "optimal_inaccurate"):
        # OSQP (ADMM) stalls on this problem at low IRT weights, where the
        # quadratic term is too small relative to the linear cost/revenue
        # terms for its step-size heuristics to behave well — confirmed by
        # testing across weights 0-50. CLARABEL (interior-point) doesn't
        # have that sensitivity; kept as the fallback rather than the
        # default so the common case still uses the solver Build Spec
        # §5.3 specifies.
        problem.solve(solver=cp.CLARABEL)
    if problem.status not in ("optimal", "optimal_inaccurate"):
        raise RuntimeError(f"convex planner failed: {problem.status}")

    out = []
    for t, iv in enumerate(intervals):
        setpoint_kw = float(discharge_kw.value[t] - charge_kw.value[t])
        out.append({
            "ts_end": iv.ts_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "setpoint_kw": round(setpoint_kw, 3),
            "mode": "normal",
            "soc_target": round(float(soc_kwh.value[t + 1] / capacity_kwh), 4),
        })
    return out


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.pv_truth import assign_pv_truth_params, generate_true_pv
    from world.weather import fetch_scenario_weather, to_15min
    from world.irt_library import build_library, tod_charge_price_paise, iex_proxy_value_paise
    from gateway.orchestrator.irt import compute_daily_features, blend_reference_trajectory
    from gateway.orchestrator.planner_rules import plan_rule_based
    from gateway.network_model import (
        build_pandapower_net, update_household_loads, run_power_flow,
        detect_violations, compute_phase_limits,
    )

    scenario = load_scenario()
    transformer_kva = scenario["neighbourhood"]["transformer_kva"]
    nominal_v_ln = scenario["neighbourhood"]["nominal_v_ln"]
    v_limit_pct = scenario["neighbourhood"]["v_limit_pct"]
    battery_by_phase = {b["phase"]: b for b in scenario["battery_blocks"]}

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

    total_kw_by_phase = feeder.household.groupby("phase")["sanctioned_load_kw"].sum().to_dict()
    net = build_pandapower_net(feeder, transformer_kva, nominal_v_ln)

    # Same synthetic evening-peak forecast used to demo planner_rules.py —
    # forecast/load_model.py isn't built yet.
    start = datetime(2026, 9, 25, 0, 15, tzinfo=timezone.utc)
    hours = np.array([(start + timedelta(minutes=15 * i)).hour + (start + timedelta(minutes=15 * i)).minute / 60
                       for i in range(96)])
    shape = 0.55 + 0.60 * np.exp(-((hours - 19.5) ** 2) / (2 * 1.2 ** 2)) \
                 - 0.15 * np.exp(-((hours - 13.0) ** 2) / (2 * 2.0 ** 2))
    shape = np.clip(shape, 0.25, None)

    ts_index = pd.DatetimeIndex([start + timedelta(minutes=15 * (i + 1)) for i in range(96)])
    charge_price = tod_charge_price_paise(ts_index)
    temp_for_day = np.full(96, weather_15min["temperature_c"].mean())  # a plausible forecast-day temp
    discharge_value = iex_proxy_value_paise(ts_index, temp_for_day, rng)

    target_features = daily_features.iloc[len(daily_features) // 3]  # an arbitrary "tomorrow"

    intervals_by_phase = {p: [] for p in ["R", "Y", "B"]}
    for i in range(96):
        load_kw = dict(zip(feeder.household["bus_id"], feeder.household["sanctioned_load_kw"] * shape[i]))
        update_household_loads(net, feeder, load_kw)
        run_power_flow(net)
        viol = detect_violations(net, v_limit_pct)
        for phase in ["R", "Y", "B"]:
            phase_viol = viol[viol.phase == phase]
            severity = float(
                ((phase_viol["voltage_v"] - nominal_v_ln).abs() / nominal_v_ln
                 - v_limit_pct / 100.0).clip(lower=0).max()
            ) if not phase_viol.empty else 0.0
            limits = compute_phase_limits(net, feeder.root, phase, v_limit_pct, battery_by_phase[phase]["power_kw"])
            intervals_by_phase[phase].append(IntervalForecast(
                ts_end=ts_index[i], max_charge_kw=limits.max_charge_kw,
                max_discharge_kw=limits.max_discharge_kw, violation_severity=severity,
            ))

    print("--- convex planner vs rule-based fallback, same forecast ---")
    for phase in ["R", "Y", "B"]:
        block = battery_by_phase[phase]
        intervals = intervals_by_phase[phase]
        reference, chosen = blend_reference_trajectory(target_features, daily_features, irt_library, phase)

        cvx_plan = plan_convex(
            capacity_kwh=block["capacity_kwh"], power_kw=block["power_kw"],
            soc_min=0.15, soc_max=0.90, efficiency=0.92, intervals=intervals,
            reserve_floor_kwh=0.15 * block["capacity_kwh"], initial_soc_frac=0.5,
            charge_price_paise=charge_price, discharge_value_paise=discharge_value,
            irt_reference_soc_frac=reference,
        )
        rule_plan = plan_rule_based(
            capacity_kwh=block["capacity_kwh"], power_kw=block["power_kw"],
            soc_max=0.90, efficiency=0.92, intervals=intervals,
            reserve_floor_kwh=0.15 * block["capacity_kwh"], initial_soc_frac=0.5,
        )

        def summarize(plan):
            n_violations = sum(1 for iv in intervals if iv.violation_severity > 0)
            served = sum(
                min(p["setpoint_kw"], iv.max_discharge_kw) * 0.25
                for p, iv in zip(plan, intervals) if iv.violation_severity > 0 and p["setpoint_kw"] > 0
            )
            peak_discharge = max((p["setpoint_kw"] for p in plan), default=0.0)
            return n_violations, served, peak_discharge

        n_viol, cvx_served, cvx_peak = summarize(cvx_plan)
        _, rule_served, rule_peak = summarize(rule_plan)
        print(f"phase {phase}: {n_viol} forecast violation intervals -- "
              f"convex serves {cvx_served:.2f} kWh (peak {cvx_peak:.2f} kW) into them, "
              f"rule-based serves {rule_served:.2f} kWh (peak {rule_peak:.2f} kW)")
