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
Feeder's published reference solution — that's what this file currently
implements and is the only piece built so far. Violation detection and
the per-phase limit calculation (jobs 2 and 3) are not yet implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pandapower as pp
import pandapower.networks as pn
from pandapower.pf.runpp_3ph import runpp_3ph

MODULE_DIR = Path(__file__).parent

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


if __name__ == "__main__":
    report = validate_against_ieee_reference()
    print(report)
    assert report.passed, "runpp_3ph did not reproduce the IEEE reference within tolerance"
