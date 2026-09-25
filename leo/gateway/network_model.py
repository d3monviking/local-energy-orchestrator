"""gateway/network_model.py — runpp_3ph, violations, per-phase safe limits.

Owner A. See Build Specification v1.0 §6.5 and System Architecture v3.0 §C6.

Three jobs, per §6.5:
  1. Simulate per-phase voltage and loading from the forecasts, flag
     violations against the network's voltage limit.
  2. Compute per-phase, per-interval maximum safe charge/discharge for
     each battery block, via voltage sensitivity rather than bisection.
  3. Re-simulate after the battery/DR plan to confirm the gap closes.

Before pointing runpp_3ph at the generated Hoskote feeder (world/feeder.py),
its solver configuration is validated against the IEEE European LV Test
Feeder's published reference solution (§6.3) — see
validate_against_ieee_reference(). build_pandapower_net() then builds the
same kind of network from world/feeder.py's output, detect_violations()
does job 1, and compute_phase_limits() does job 2 via the voltage-
sensitivity shortcut in §6.5 rather than per-interval bisection. Job 3
(re-simulate after the battery/DR plan) needs a plan, which doesn't exist
yet — that lands with the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pandapower as pp
import pandapower.networks as pn
from pandapower.pf.runpp_3ph import runpp_3ph

MODULE_DIR = Path(__file__).parent

PHASE_LETTER = {"R": "a", "Y": "b", "B": "c"}

# (verify) Small distribution transformer defaults — scenario.yaml only
# pins vk_percent (~4.5%, from System Architecture v3.0 §7); the rest are
# typical figures for a small oil-filled Dyn unit, not a datasheet.
# Zero-sequence fields follow the same shape pandapower's own IEEE European
# LV network uses (vk0=vk, vkr0=vkr, mag0_percent=100, mag0_rx=0).
DEFAULT_TRAFO_PARAMS = dict(
    vkr_percent=1.0,
    vkr0_percent=1.0,
    pfe_kw=0.1,
    i0_percent=0.3,
    shift_degree=330.0,   # Dyn11, the common Indian distribution convention
    vector_group="Dyn",
    mag0_percent=100.0,
    mag0_rx=0.0,
    si0_hv_partial=0.9,
)

# (verify) IS 14255 zero-sequence R/X is not sourced (see world/feeder.py's
# CONDUCTOR_LIBRARY note); these multipliers are typical for 4-wire LV ABC
# with a bundled neutral, where zero-sequence current returns mostly via
# that neutral rather than earth, so x0/x1 stays close to 1.
ZERO_SEQ_R_MULTIPLIER = 3.0
ZERO_SEQ_X_MULTIPLIER = 1.2

DEFAULT_POWER_FACTOR = 0.95
HV_SLACK_VM_PU = 1.0  # (verify) assumed DISCOM 11 kV side voltage

# The three official IEEE snapshot scenarios, and our cached reference
# extract (from Solutions/OpenDSS/Snapshots/*.xlsx in the official package
# at https://cmte.ieee.org/pes-testfeeders/resources/, European LV Test
# Feeder v2). Only on_peak_566 is cached today; the other two scenarios can
# be added the same way if a second validation snapshot is wanted.
REFERENCE_FILES = {
    "on_peak_566": MODULE_DIR / "ieee_lv_reference_on_peak_566.csv",
}

# Observed max error against the official OpenDSS solution is ~0.74% pu
# (mean ~0.43%), attributable to solver and line-model differences between
# OpenDSS and pandapower's converter, not a configuration bug. 1% pu is a
# safe validation tolerance above that observed ceiling.
VOLTAGE_TOLERANCE_PU = 0.01


@dataclass
class ValidationReport:
    scenario: str
    n_comparisons: int
    max_abs_error_pu: float
    mean_abs_error_pu: float
    tolerance_pu: float

    @property
    def passed(self) -> bool:
        return self.max_abs_error_pu <= self.tolerance_pu

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] runpp_3ph vs IEEE European LV Test Feeder ({self.scenario}): "
            f"max |err| = {self.max_abs_error_pu*100:.3f}% pu, "
            f"mean |err| = {self.mean_abs_error_pu*100:.3f}% pu "
            f"over {self.n_comparisons} bus-phase comparisons "
            f"(tolerance {self.tolerance_pu*100:.1f}% pu)"
        )


def validate_against_ieee_reference(
    scenario: str = "on_peak_566", tolerance_pu: float = VOLTAGE_TOLERANCE_PU
) -> ValidationReport:
    """Run `runpp_3ph` on pandapower's built-in IEEE European LV Test Feeder
    and compare per-bus, per-phase voltage magnitudes against the official
    OpenDSS reference solution, per Build Spec §6.3.

    This is the one-time sanity check that must pass before the solver is
    pointed at the generated Hoskote feeder — it confirms the three-phase
    unbalanced power flow is configured correctly on a network with a known
    answer, before running it on one with no reference answer.
    """
    if scenario not in REFERENCE_FILES:
        raise ValueError(f"no cached reference for scenario {scenario!r}")

    net = pn.ieee_european_lv_asymmetric(scenario)
    pp.reset_results(net, mode="pf_3ph")
    runpp_3ph(net)

    ref = pd.read_csv(REFERENCE_FILES[scenario], dtype={"bus_name": str})
    ref = ref.set_index("bus_name")
    bus_idx_by_name = dict(zip(net.bus["name"].astype(str), net.bus.index))

    errors = []
    for bus_name, row in ref.iterrows():
        idx = bus_idx_by_name[bus_name]
        result = net.res_bus_3ph.loc[idx]
        for phase, col in [("a", "vm_a_pu"), ("b", "vm_b_pu"), ("c", "vm_c_pu")]:
            errors.append(abs(result[col] - row[f"vm_{phase}_pu"]))

    errors = np.array(errors)
    return ValidationReport(
        scenario=scenario,
        n_comparisons=len(errors),
        max_abs_error_pu=float(errors.max()),
        mean_abs_error_pu=float(errors.mean()),
        tolerance_pu=tolerance_pu,
    )


def get_bus_index(net, bus_id: str) -> int:
    matches = net.bus.index[net.bus["name"] == str(bus_id)]
    if len(matches) == 0:
        raise KeyError(f"no bus named {bus_id!r} in net")
    return int(matches[0])


def build_pandapower_net(
    feeder,  # world.feeder.FeederResult
    transformer_kva: float,
    nominal_v_ln: float,
    vk_percent: float = 4.5,
    household_load_kw: Optional[dict[str, float]] = None,
    power_factor: float = DEFAULT_POWER_FACTOR,
):
    """Build a pandapower net for `runpp_3ph` from a world/feeder.py
    FeederResult: an 11 kV slack, the distribution transformer, every
    bus/line from the generated feeder, and an asymmetric load per
    household on its assigned phase.

    `household_load_kw` maps household bus_id -> real power draw in kW
    (positive = consuming); households not present draw zero. This is a
    caller-supplied snapshot (a forecast interval, or a stress-test value
    for demonstration) — world/households.py's truth-side load timeseries
    is a separate, not-yet-built module.
    """
    household_load_kw = household_load_kw or {}
    vn_lv_kv = nominal_v_ln * np.sqrt(3) / 1000.0

    net = pp.create_empty_network()
    hv_bus = pp.create_bus(net, vn_kv=11.0, name="HV-SOURCE")
    pp.create_ext_grid(
        net, bus=hv_bus, vm_pu=HV_SLACK_VM_PU, name="grid",
        # (verify) treats the upstream 11 kV grid as stiff relative to a
        # 100 kVA transformer — same short-circuit ratio convention as
        # pandapower's own IEEE European LV network fixture.
        s_sc_max_mva=1000.0, s_sc_min_mva=800.0,
        rx_max=0.1, rx_min=0.1, r0x0_max=0.1, x0x_max=1.0, r0x0_min=0.1, x0x_min=1.0,
    )

    bus_idx = {}
    for _, row in feeder.bus.iterrows():
        bus_idx[row["id"]] = pp.create_bus(net, vn_kv=vn_lv_kv, name=row["id"])

    lv_root_bus = bus_idx[str(feeder.root)]
    pp.create_transformer_from_parameters(
        net,
        hv_bus=hv_bus,
        lv_bus=lv_root_bus,
        sn_mva=transformer_kva / 1000.0,
        vn_hv_kv=11.0,
        vn_lv_kv=vn_lv_kv,
        vk_percent=vk_percent,
        vk0_percent=vk_percent,
        name="DT",
        **DEFAULT_TRAFO_PARAMS,
    )

    for _, row in feeder.line.iterrows():
        r1, x1 = row["r_ohm_per_km"], row["x_ohm_per_km"]
        pp.create_line_from_parameters(
            net,
            from_bus=bus_idx[row["from_bus"]],
            to_bus=bus_idx[row["to_bus"]],
            length_km=row["length_m"] / 1000.0,
            r_ohm_per_km=r1,
            x_ohm_per_km=x1,
            c_nf_per_km=0.0,
            max_i_ka=row["ampacity_a"] / 1000.0,
            r0_ohm_per_km=r1 * ZERO_SEQ_R_MULTIPLIER,
            x0_ohm_per_km=x1 * ZERO_SEQ_X_MULTIPLIER,
            c0_nf_per_km=0.0,
            name=row["id"],
        )

    q_factor = np.tan(np.arccos(power_factor))
    for _, row in feeder.household.iterrows():
        p_kw = household_load_kw.get(row["bus_id"], 0.0)
        letter = PHASE_LETTER[row["phase"]]
        pp.create_asymmetric_load(
            net,
            bus=bus_idx[row["bus_id"]],
            **{f"p_{letter}_mw": p_kw / 1000.0, f"q_{letter}_mvar": p_kw * q_factor / 1000.0},
            name=row["id"],
        )

    return net


def update_household_loads(
    net, feeder, household_load_kw: dict[str, float], power_factor: float = DEFAULT_POWER_FACTOR
) -> None:
    """Mutate an existing net's asymmetric_load table in place for a new
    interval's household loads, instead of rebuilding the whole net via
    build_pandapower_net() — reconstructing a ~160-bus net from scratch
    costs ~1s in this pandapower version, which is fine once but far too
    slow to repeat per forecast interval."""
    q_factor = np.tan(np.arccos(power_factor))
    bus_by_load_name = dict(zip(feeder.household["id"], feeder.household["bus_id"]))
    phase_by_load_name = dict(zip(feeder.household["id"], feeder.household["phase"]))
    load_idx_by_name = dict(zip(net.asymmetric_load["name"], net.asymmetric_load.index))

    for hh_id, load_idx in load_idx_by_name.items():
        p_kw = household_load_kw.get(bus_by_load_name[hh_id], 0.0)
        active_letter = PHASE_LETTER[phase_by_load_name[hh_id]]
        for letter in ("a", "b", "c"):
            on_this_phase = letter == active_letter
            net.asymmetric_load.at[load_idx, f"p_{letter}_mw"] = p_kw / 1000.0 if on_this_phase else 0.0
            net.asymmetric_load.at[load_idx, f"q_{letter}_mvar"] = (
                p_kw * q_factor / 1000.0 if on_this_phase else 0.0
            )


def run_power_flow(net) -> None:
    pp.reset_results(net, mode="pf_3ph")
    runpp_3ph(net)


def detect_violations(net, v_limit_pct: float) -> pd.DataFrame:
    """Job 1: per-bus, per-phase voltage and the upstream line's loading,
    flagged against the network's voltage limit. One row per (bus, phase),
    matching the `network_result` table shape."""
    line_to_bus = dict(zip(net.line["to_bus"], net.line.index))
    rows = []
    for bus_idx_, bus_row in net.bus.iterrows():
        if bus_row["name"] == "HV-SOURCE" or bus_idx_ not in net.res_bus_3ph.index:
            continue  # the 11 kV slack bus isn't part of the feeder's own bus table
        res = net.res_bus_3ph.loc[bus_idx_]
        line_idx = line_to_bus.get(bus_idx_)
        for phase, letter in PHASE_LETTER.items():
            vm_pu = res[f"vm_{letter}_pu"]
            loading_pct = None
            if line_idx is not None:
                loading_pct = net.res_line_3ph.loc[line_idx, f"loading_{letter}_percent"]
            rows.append({
                "bus_id": bus_row["name"],
                "phase": phase,
                "voltage_v": vm_pu * (net.bus.loc[bus_idx_, "vn_kv"] * 1000.0 / np.sqrt(3)),
                "loading_pct": loading_pct,
                "violation": bool(abs(vm_pu - 1.0) > v_limit_pct / 100.0),
            })
    return pd.DataFrame(rows)


@dataclass
class PhaseLimit:
    phase: str
    max_charge_kw: float
    max_discharge_kw: float
    v_base_pu: float
    dv_dp_pu_per_kw: float


def compute_phase_limits(
    net,
    battery_bus_id: str,
    phase: str,
    v_limit_pct: float,
    inverter_kw: float,
    probe_step_kw: float = 1.0,
) -> PhaseLimit:
    """Job 2, §6.5: how far this phase's battery block may charge or
    discharge before it causes a violation itself, via one extra solve
    (voltage sensitivity) rather than a bisection search per direction.

    `battery_bus_id` is assumed to be the shared-storage connection point
    (the transformer/LV-busbar bus in this prototype — see System
    Architecture v3.0 §7 on where the three blocks are sited).
    """
    letter = PHASE_LETTER[phase]
    idx = get_bus_index(net, battery_bus_id)

    run_power_flow(net)
    v_base = float(net.res_bus_3ph.loc[idx, f"vm_{letter}_pu"])

    sgen_idx = pp.create_asymmetric_sgen(net, bus=idx, **{f"p_{letter}_mw": probe_step_kw / 1000.0})
    run_power_flow(net)
    v_probe = float(net.res_bus_3ph.loc[idx, f"vm_{letter}_pu"])
    net.asymmetric_sgen.drop(sgen_idx, inplace=True)

    dv_dp = (v_probe - v_base) / probe_step_kw  # pu per kW; + injecting raises voltage

    v_upper = 1.0 + v_limit_pct / 100.0
    v_lower = 1.0 - v_limit_pct / 100.0

    if dv_dp > 1e-9:
        max_discharge = (v_upper - v_base) / dv_dp
        max_charge = (v_base - v_lower) / dv_dp
    else:
        # No measurable sensitivity at this bus/step size -> the inverter
        # rating is the only binding constraint.
        max_discharge = inverter_kw
        max_charge = inverter_kw

    max_discharge = float(np.clip(max_discharge, 0.0, inverter_kw))
    max_charge = float(np.clip(max_charge, 0.0, inverter_kw))

    run_power_flow(net)  # leave the net clean (no probe sgen) for the caller

    return PhaseLimit(
        phase=phase,
        max_charge_kw=max_charge,
        max_discharge_kw=max_discharge,
        v_base_pu=v_base,
        dv_dp_pu_per_kw=dv_dp,
    )


def verify_phase_limit(
    net, battery_bus_id: str, phase: str, dispatch_kw: float, v_limit_pct: float
) -> tuple[bool, Optional[str], float]:
    """Full-solve verification of a proposed dispatch (§6.5: "verify only
    the binding intervals with a full solve"). Dispatches `dispatch_kw` at
    the battery bus (+discharge, -charge) and checks EVERY bus/phase on
    this phase, not just the battery's own bus, since the analytical
    estimate in compute_phase_limits() only looks at local sensitivity.

    `ok=False` means a violation exists somewhere on this phase WITH this
    dispatch applied — it may be one the dispatch improved but didn't fully
    resolve (an already-overloaded feeder), not necessarily one the
    dispatch caused. That distinction is exactly job 3's "residual gap":
    the caller escalates it to the recommendation engine rather than
    treating it as a rejected dispatch.

    Returns (ok, worst_bus_id, worst_vm_pu) — the worst bus is the one
    furthest from nominal voltage, not merely furthest from the phase mean.
    """
    letter = PHASE_LETTER[phase]
    idx = get_bus_index(net, battery_bus_id)
    sgen_idx = pp.create_asymmetric_sgen(net, bus=idx, **{f"p_{letter}_mw": dispatch_kw / 1000.0})
    run_power_flow(net)

    violations = detect_violations(net, v_limit_pct)
    violations = violations[violations["phase"] == phase]
    v_nom = net.bus.loc[idx, "vn_kv"] * 1000.0 / np.sqrt(3)

    net.asymmetric_sgen.drop(sgen_idx, inplace=True)
    run_power_flow(net)

    violating = violations[violations["violation"]]
    if violating.empty:
        return True, None, 1.0

    worst = violating.loc[(violating["voltage_v"] - v_nom).abs().idxmax()]
    return False, worst["bus_id"], float(worst["voltage_v"] / v_nom)


if __name__ == "__main__":
    report = validate_against_ieee_reference()
    print(report)
    assert report.passed, "runpp_3ph did not reproduce the IEEE reference within tolerance"

    print()
    print("--- generated Hoskote feeder ---")
    from world.feeder import build_feeder, load_scenario  # gateway consumes world's output, not vice versa

    scenario = load_scenario()
    transformer_kva = scenario["neighbourhood"]["transformer_kva"]
    nominal_v_ln = scenario["neighbourhood"]["nominal_v_ln"]
    v_limit_pct = scenario["neighbourhood"]["v_limit_pct"]
    battery_kw_by_phase = {b["phase"]: b["power_kw"] for b in scenario["battery_blocks"]}

    feeder = build_feeder(scenario)
    net = build_pandapower_net(feeder, transformer_kva=transformer_kva, nominal_v_ln=nominal_v_ln)
    run_power_flow(net)
    baseline = detect_violations(net, v_limit_pct=v_limit_pct)
    print(f"zero-load sanity: {baseline.violation.sum()} violations "
          f"(expect 0), voltage range {baseline.voltage_v.min():.2f}-{baseline.voltage_v.max():.2f} V")

    # A stress-test load (1.2x sanctioned demand on every household) —
    # world/households.py's appliance-level truth model isn't built yet, so
    # this is a synthetic scenario purely to exercise violation detection
    # and the phase-limit calculation end to end on the real feeder, not a
    # claim about realistic evening-peak demand.
    factor = 1.2
    stress_load = dict(zip(feeder.household["bus_id"], feeder.household["sanctioned_load_kw"] * factor))
    net = build_pandapower_net(
        feeder, transformer_kva=transformer_kva, nominal_v_ln=nominal_v_ln, household_load_kw=stress_load
    )
    run_power_flow(net)
    stressed = detect_violations(net, v_limit_pct=v_limit_pct)
    print(f"stress test ({factor}x sanctioned load): "
          f"{stressed.violation.sum()}/{len(stressed)} bus-phase violations")

    print()
    for phase in ["R", "Y", "B"]:
        n_before = int(stressed[(stressed.phase == phase) & stressed.violation].shape[0])
        limit = compute_phase_limits(
            net, battery_bus_id=feeder.root, phase=phase, v_limit_pct=v_limit_pct,
            inverter_kw=battery_kw_by_phase[phase],
        )
        ok, worst_bus, worst_vm = verify_phase_limit(
            net, feeder.root, phase, limit.max_discharge_kw, v_limit_pct
        )
        resolved = "fully resolved" if ok else f"residual gap at {worst_bus} ({worst_vm:.3f} pu)"
        print(
            f"phase {phase}: {n_before:3d} violations before battery -> "
            f"max_discharge={limit.max_discharge_kw:.2f}kW max_charge={limit.max_charge_kw:.2f}kW "
            f"-> {resolved}"
        )
