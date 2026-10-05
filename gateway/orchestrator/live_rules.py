"""gateway/orchestrator/live_rules.py — live voltage correction.

Owner A. See System Architecture v3.0 §9.2.

Per phase, every few seconds to a minute:

    far-end voltage below the undervoltage threshold
        -> discharge more than planned, within the safe limit
    far-end voltage above the overvoltage threshold
        -> charge more than planned, within the safe limit
    otherwise
        -> follow the approved plan

Simple on purpose: a transparent rule reacting to a few trustworthy live
readings is more robust than a complex model reacting to guesses, and
these voltage swings happen faster than any planning cycle could react to.
This is the ONLY thing that may deviate from the approved plan within the
day — it never re-plans, it only nudges the current setpoint within the
safe limits the network model already computed for this interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LiveCorrection:
    setpoint_kw: float
    rule_triggered: Optional[str]  # 'undervoltage' | 'overvoltage' | None


def apply_live_rule(
    planned_setpoint_kw: float,
    far_end_voltage_v: float,
    nominal_v_ln: float,
    v_limit_pct: float,
    max_charge_kw: float,
    max_discharge_kw: float,
) -> LiveCorrection:
    """Nudge the approved plan's setpoint using the far-end sensor reading
    for this phase. Never exceeds the safe per-phase limits the network
    model computed for this interval — a live correction that itself
    causes a violation elsewhere defeats the point.
    """
    lower = nominal_v_ln * (1 - v_limit_pct / 100.0)
    upper = nominal_v_ln * (1 + v_limit_pct / 100.0)

    if far_end_voltage_v < lower:
        # Discharge as hard as the safe limit allows — undervoltage at the
        # far end is the exact condition the battery exists to relieve.
        return LiveCorrection(setpoint_kw=max_discharge_kw, rule_triggered="undervoltage")

    if far_end_voltage_v > upper:
        setpoint_kw = -max_charge_kw
        return LiveCorrection(setpoint_kw=setpoint_kw, rule_triggered="overvoltage")

    return LiveCorrection(setpoint_kw=planned_setpoint_kw, rule_triggered=None)


if __name__ == "__main__":
    nominal_v_ln = 250.0
    v_limit_pct = 6.0

    cases = [
        ("normal", 248.0, 3.0),
        ("undervoltage", 232.0, 3.0),
        ("overvoltage", 268.0, -1.0),
    ]
    for label, v_far_end, planned in cases:
        result = apply_live_rule(
            planned_setpoint_kw=planned, far_end_voltage_v=v_far_end,
            nominal_v_ln=nominal_v_ln, v_limit_pct=v_limit_pct,
            max_charge_kw=7.5, max_discharge_kw=7.5,
        )
        print(f"{label:>13}: far-end={v_far_end}V planned={planned}kW "
              f"-> setpoint={result.setpoint_kw}kW rule={result.rule_triggered}")
