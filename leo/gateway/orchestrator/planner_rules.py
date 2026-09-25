"""gateway/orchestrator/planner_rules.py — rule-based fallback battery plan.

Owner A. See Build Specification v1.0 §5.3 and System Architecture v3.0 §9.1.

Build the fallback first. Both this and planner_cvx.py implement the same
interface (a list of IntervalForecast in, a 96-interval plan out); the
orchestrator picks between them by config.

Rule: charge during the midday surplus window up to the per-phase charge
limit and the SoC ceiling; discharge during forecast violation windows up
to the discharge limit, prioritising the largest forecast gap; hold the
reserve floor at all times.

"Largest forecast gap" is read here as the worst voltage excursion beyond
the limit on that phase in that interval (violation_severity) — the
network model's job 1 output, not a separately-modelled kW deficit. Within
a contiguous run of violation intervals, discharge is allocated to the
worst intervals first, bounded by whatever energy is actually available
at that point in the day; outside a violation run, the plan is a single
chronological pass, so causality (you can't spend energy you haven't
charged yet) always holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class IntervalForecast:
    ts_end: datetime
    max_charge_kw: float       # from network_model.compute_phase_limits, positive magnitude
    max_discharge_kw: float    # from network_model.compute_phase_limits, positive magnitude
    violation_severity: float = 0.0  # 0 = no forecast violation this interval; else pu beyond the limit


def _contiguous_violation_blocks(intervals: list[IntervalForecast]) -> list[list[int]]:
    blocks: list[list[int]] = []
    current: list[int] = []
    for i, iv in enumerate(intervals):
        if iv.violation_severity > 0:
            current.append(i)
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


def plan_rule_based(
    capacity_kwh: float,
    power_kw: float,
    soc_max: float,
    efficiency: float,
    intervals: list[IntervalForecast],
    reserve_floor_kwh: float,
    initial_soc_frac: float,
    interval_hours: float = 0.25,
    charge_window_hours: tuple[int, int] = (10, 15),
) -> list[dict]:
    """The ~80-line fallback. Returns a list of dicts matching the
    `intervals` items in contracts/schemas/plan.schema.json (ts_end,
    setpoint_kw, mode, soc_target). Sign convention: negative = charging,
    positive = discharging, same as everywhere else in LEO.
    """
    n = len(intervals)
    setpoint_kw = [0.0] * n
    soc_kwh = initial_soc_frac * capacity_kwh
    soc_ceiling_kwh = soc_max * capacity_kwh

    blocks = _contiguous_violation_blocks(intervals)
    block_start = {b[0]: b for b in blocks}

    i = 0
    while i < n:
        if i in block_start:
            block = block_start[i]
            for j in sorted(block, key=lambda k: -intervals[k].violation_severity):
                headroom_kwh = soc_kwh - reserve_floor_kwh
                if headroom_kwh <= 0:
                    continue
                jv = intervals[j]
                discharge_kw = max(0.0, min(jv.max_discharge_kw, power_kw, headroom_kwh / interval_hours))
                setpoint_kw[j] = discharge_kw
                soc_kwh -= discharge_kw * interval_hours
            i = block[-1] + 1
            continue

        iv = intervals[i]
        if charge_window_hours[0] <= iv.ts_end.hour < charge_window_hours[1]:
            headroom_kwh = soc_ceiling_kwh - soc_kwh
            charge_kw = max(0.0, min(iv.max_charge_kw, power_kw, headroom_kwh / (interval_hours * efficiency)))
            setpoint_kw[i] = -charge_kw
            soc_kwh += charge_kw * interval_hours * efficiency
        i += 1

    # Chronological pass to report the SoC trajectory. setpoint_kw is
    # already final at this point (block allocation above is order-
    # independent on total energy moved; this pass is for reporting only).
    running_soc_kwh = initial_soc_frac * capacity_kwh
    out = []
    for i, iv in enumerate(intervals):
        kw = setpoint_kw[i]
        if kw < 0:
            running_soc_kwh += (-kw) * interval_hours * efficiency
        else:
            running_soc_kwh -= kw * interval_hours
        out.append({
            "ts_end": iv.ts_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "setpoint_kw": round(kw, 3),
            "mode": "normal",  # pre_outage/backup/restoration are orchestrator/modes.py's call
            "soc_target": round(running_soc_kwh / capacity_kwh, 4),
        })
    return out


if __name__ == "__main__":
    import time

    import numpy as np
    import pandapower as pp
    from datetime import timedelta, timezone

    from world.feeder import build_feeder, load_scenario
    from gateway.network_model import (
        build_pandapower_net, update_household_loads, run_power_flow,
        detect_violations, compute_phase_limits, get_bus_index,
    )

    scenario = load_scenario()
    transformer_kva = scenario["neighbourhood"]["transformer_kva"]
    nominal_v_ln = scenario["neighbourhood"]["nominal_v_ln"]
    v_limit_pct = scenario["neighbourhood"]["v_limit_pct"]
    battery_by_phase = {b["phase"]: b for b in scenario["battery_blocks"]}

    feeder = build_feeder(scenario)
    total_kw_by_phase = feeder.household.groupby("phase")["sanctioned_load_kw"].sum().to_dict()
    net = build_pandapower_net(feeder, transformer_kva, nominal_v_ln)  # built once, mutated per interval

    # Synthetic day-ahead forecast: forecast/load_model.py isn't built yet.
    # Stylised shape only — midday PV-offset dip, evening peak — scaled per
    # phase from that phase's connected sanctioned load, purely to exercise
    # the planner end to end on the real feeder.
    start = datetime(2026, 9, 25, 0, 15, tzinfo=timezone.utc)
    hours = np.array([(start + timedelta(minutes=15 * i)).hour + (start + timedelta(minutes=15 * i)).minute / 60
                       for i in range(96)])
    shape = 0.55 + 0.60 * np.exp(-((hours - 19.5) ** 2) / (2 * 1.2 ** 2)) \
                 - 0.15 * np.exp(-((hours - 13.0) ** 2) / (2 * 2.0 ** 2))
    shape = np.clip(shape, 0.25, None)

    t0 = time.perf_counter()
    intervals_by_phase = {p: [] for p in ["R", "Y", "B"]}
    for i in range(96):
        ts_end = start + timedelta(minutes=15 * (i + 1))
        load = dict(zip(
            feeder.household["bus_id"],
            feeder.household["sanctioned_load_kw"] * shape[i],
        ))
        update_household_loads(net, feeder, load)
        run_power_flow(net)
        viol = detect_violations(net, v_limit_pct)

        for phase in ["R", "Y", "B"]:
            phase_viol = viol[viol.phase == phase]
            severity = float(
                ((phase_viol["voltage_v"] - nominal_v_ln).abs() / nominal_v_ln
                 - v_limit_pct / 100.0).clip(lower=0).max()
            ) if not phase_viol.empty else 0.0
            limits = compute_phase_limits(
                net, feeder.root, phase, v_limit_pct, battery_by_phase[phase]["power_kw"]
            )
            intervals_by_phase[phase].append(IntervalForecast(
                ts_end=ts_end, max_charge_kw=limits.max_charge_kw,
                max_discharge_kw=limits.max_discharge_kw, violation_severity=severity,
            ))
    print(f"96-interval x 3-phase network sweep: {time.perf_counter()-t0:.1f}s")

    print("--- per-phase rule-based plan on a synthetic evening-peak forecast ---")
    plans = {}
    for phase in ["R", "Y", "B"]:
        block = battery_by_phase[phase]
        intervals = intervals_by_phase[phase]
        n_violations_before = sum(1 for iv in intervals if iv.violation_severity > 0)
        plan = plan_rule_based(
            capacity_kwh=block["capacity_kwh"], power_kw=block["power_kw"],
            soc_max=0.90, efficiency=0.92, intervals=intervals,
            reserve_floor_kwh=0.15 * block["capacity_kwh"], initial_soc_frac=0.5,
        )
        plans[phase] = plan
        n_charging = sum(1 for p in plan if p["setpoint_kw"] < 0)
        n_discharging = sum(1 for p in plan if p["setpoint_kw"] > 0)
        peak_discharge = max((p["setpoint_kw"] for p in plan), default=0.0)
        print(f"phase {phase}: {n_violations_before} intervals forecast to violate, "
              f"plan charges in {n_charging} intervals, discharges in {n_discharging} "
              f"(peak {peak_discharge:.2f} kW)")

    # Job 3: re-simulate with the plan's dispatch applied and confirm the
    # forecast violations actually close.
    print()
    print("--- re-simulating the day WITH the plan applied (job 3) ---")
    total_before = total_after = 0
    for i in range(96):
        load = dict(zip(feeder.household["bus_id"], feeder.household["sanctioned_load_kw"] * shape[i]))
        update_household_loads(net, feeder, load)
        run_power_flow(net)
        before = detect_violations(net, v_limit_pct)
        total_before += int(before.violation.sum())

        idx = get_bus_index(net, feeder.root)
        for phase in ["R", "Y", "B"]:
            dispatch_kw = plans[phase][i]["setpoint_kw"]
            if dispatch_kw != 0:
                letter = {"R": "a", "Y": "b", "B": "c"}[phase]
                pp.create_asymmetric_sgen(net, bus=idx, **{f"p_{letter}_mw": dispatch_kw / 1000.0})
        run_power_flow(net)
        after = detect_violations(net, v_limit_pct)
        total_after += int(after.violation.sum())
        # drop the sgens created above before the next interval
        drop_idx = net.asymmetric_sgen.index[net.asymmetric_sgen.index >= 0]
        if len(drop_idx) > 0:
            net.asymmetric_sgen.drop(drop_idx, inplace=True)

    print(f"total bus-phase-intervals violating WITHOUT the plan: {total_before}")
    print(f"total bus-phase-intervals violating WITH the plan:    {total_after}")
