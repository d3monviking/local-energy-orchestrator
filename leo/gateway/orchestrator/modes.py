"""gateway/orchestrator/modes.py — four-mode state machine, reserve floor.

Owner A. See System Architecture v3.0 §9.3.

    Normal --> PreOutage   (C7, forecast trigger)
    PreOutage --> Normal   (risk clears)
    PreOutage --> Backup   (C9, grid lost)
    Normal --> Backup      (C9, grid lost)
    Backup --> Restoration (C9, grid returns)
    Restoration --> Normal (staggering complete)

Ownership: C7 (this file's caller, the forecast side) owns Normal and
Pre-outage; C9 (outage detection) owns Backup and Restoration, because it
holds the events. `reserve_floor = max(C7_pre_outage_floor, C9_instruction)`
— C7 may raise the floor above C9's instruction, never below it.

`idle` (the Modbus register's off-state on a watchdog timeout) is not one
of these four modes — it's a hardware safety fallback, not a planned state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

MODES = ("normal", "pre_outage", "backup", "restoration")

ALLOWED_TRANSITIONS = {
    "normal": {"pre_outage", "backup"},
    "pre_outage": {"normal", "backup"},
    "backup": {"restoration"},
    "restoration": {"normal"},
}

OWNER_OF_TARGET = {
    "normal": "C7",
    "pre_outage": "C7",
    "backup": "C9",
    "restoration": "C9",
}


@dataclass
class ModeState:
    mode: str
    c7_pre_outage_floor_kwh: float = 0.0
    c9_reserve_instruction_kwh: float = 0.0

    @property
    def reserve_floor_kwh(self) -> float:
        return max(self.c7_pre_outage_floor_kwh, self.c9_reserve_instruction_kwh)


@dataclass
class ModeTransition:
    ts: datetime
    from_mode: str
    to_mode: str
    owner: str
    reason: str


def pre_outage_trigger(
    shedding_notice: bool, iex_price_paise_kwh: float, trailing_p90_paise_kwh: float
) -> bool:
    """§9.3: a published DISCOM load-shedding notice, or an IEX real-time
    price above the 90th percentile of the trailing 30 days. Deterministic
    and implementable today; the logistic risk classifier stays future
    scope (§10.5)."""
    return shedding_notice or iex_price_paise_kwh > trailing_p90_paise_kwh


def transition(
    state: ModeState,
    ts: datetime,
    *,
    grid_lost: bool = False,
    grid_returned: bool = False,
    restoration_complete: bool = False,
    pre_outage_risk: bool = False,
    risk_cleared: bool = False,
    c7_pre_outage_floor_kwh: Optional[float] = None,
    c9_reserve_instruction_kwh: Optional[float] = None,
) -> tuple[ModeState, Optional[ModeTransition]]:
    """One state-machine step. At most one transition fires per call —
    if grid is lost, that always wins (safety over planning), otherwise
    the mode-specific trigger for the current state is checked.

    Returns the (possibly unchanged) state and a ModeTransition record if
    one fired, for the `mode_transition` table.
    """
    if c7_pre_outage_floor_kwh is not None:
        state.c7_pre_outage_floor_kwh = c7_pre_outage_floor_kwh
    if c9_reserve_instruction_kwh is not None:
        state.c9_reserve_instruction_kwh = c9_reserve_instruction_kwh

    target: Optional[str] = None
    reason = ""

    if grid_lost and state.mode in ("normal", "pre_outage"):
        target, reason = "backup", "grid lost"
    elif grid_returned and state.mode == "backup":
        target, reason = "restoration", "grid returned"
    elif restoration_complete and state.mode == "restoration":
        target, reason = "normal", "staggered re-transfer complete"
    elif state.mode == "normal" and pre_outage_risk:
        target, reason = "pre_outage", "forecast trigger"
    elif state.mode == "pre_outage" and risk_cleared:
        target, reason = "normal", "risk cleared"

    if target is None:
        return state, None

    if target not in ALLOWED_TRANSITIONS[state.mode]:
        raise ValueError(f"illegal transition {state.mode} -> {target}")

    record = ModeTransition(
        ts=ts, from_mode=state.mode, to_mode=target, owner=OWNER_OF_TARGET[target], reason=reason
    )
    state.mode = target
    return state, record


if __name__ == "__main__":
    from datetime import timedelta

    t0 = datetime(2026, 9, 25, 18, 0)
    state = ModeState(mode="normal", c7_pre_outage_floor_kwh=2.0)
    log = []

    state, tr = transition(state, t0, pre_outage_risk=True)
    log.append(tr)
    state, tr = transition(state, t0 + timedelta(minutes=100), grid_lost=True)
    log.append(tr)
    state, tr = transition(
        state, t0 + timedelta(hours=2), grid_returned=True, c9_reserve_instruction_kwh=5.0
    )
    log.append(tr)
    state, tr = transition(state, t0 + timedelta(hours=2, minutes=20), restoration_complete=True)
    log.append(tr)

    for tr in log:
        print(f"{tr.ts}  {tr.from_mode:>11} -> {tr.to_mode:<11} owner={tr.owner}  ({tr.reason})")
    print(f"final reserve floor: {state.reserve_floor_kwh} kWh")
