"""gateway/controller.py — LEO's live decision core: C7 (live battery
correction, Normal / Pre-outage) and C9 (outage detection, Backup,
Restoration). Pure logic, no I/O.

Two callers run exactly this code:

  gateway/service.py  the edge gateway: feeds it what arrives over MQTT
                      (sensor uplinks, power-fail events) and Modbus
                      (battery and premise-meter telemetry), and writes
                      what it returns back to the devices and to Postgres.
  eval/sweep.py       the year-sampled evaluation: feeds `live_decision`
                      the same observations in-process, so the published
                      numbers come from the decisions the gateway makes.

What LEO is allowed to know here is only what its own devices report:
the busbar CT (phase feeder load, which excludes the block itself), the
busbar and far-end voltage sensors, sensor power-fail / restore events,
and the inverters' own state of charge. Never the simulated truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from gateway.orchestrator.modes import ModeState, ModeTransition, transition
from gateway.orchestrator.planner_shave import live_setpoint
from gateway.outage import allocate_backup_power, classify_outage, stage_restoration

PHASES = ("R", "Y", "B")
DT_H = 0.25
INTERVAL = timedelta(minutes=15)

SOC_BACKUP_FLOOR = 0.05      # in Backup the BMS lets the block dig below the daily 15% floor
PRE_OUTAGE_SOC_CAP = 0.90    # Pre-outage never asks for more than a full block
PRE_OUTAGE_MARGIN = 1.5      # reserve = 1.5 x the backup premises' allowance for the announced cut
# (verify) How long C9 plans an unannounced outage to last when spreading
# the remaining charge over it. ESMI-style medians are ~35 min with a long
# tail; planning for 2 h keeps the clinic powered through most of the tail.
# If the outage outlasts the estimate, the estimate is pushed out.
UNPLANNED_EXPECTED = timedelta(hours=2)
UNPLANNED_EXTENSION = timedelta(minutes=30)
RESTORATION_BATCH_GAP = timedelta(minutes=3)

CAUSE_BY_CLASS = {"upstream": "upstream_fault", "phase_fault": "phase_fault", "local": "local_fault"}


@dataclass(frozen=True)
class BlockSpec:
    phase: str
    capacity_kwh: float
    power_kw: float
    soc_min: float = 0.15
    soc_max: float = 0.90
    efficiency: float = 0.92


@dataclass
class Decision:
    setpoint_kw: float          # + discharge, - charge
    rule: Optional[str]         # why, for dispatch.rule_triggered and the console
    mode: str                   # dispatch.mode for this block
    max_charge_kw: float = 0.0  # safe limits the setpoint was clipped to
    max_discharge_kw: float = 0.0


@dataclass
class EventRecord:
    ts: datetime
    kind: str                   # event_kind_t
    scope: str
    payload: dict
    source: str = "gateway"


def pre_outage_reserve_soc(block: BlockSpec, backup_kw_on_phase: float, cut_hours: float) -> float:
    """§9.3 Pre-outage floor: what this phase's backup premises can draw
    through the announced cut, plus a 50% margin, above the backup floor."""
    need_kwh = PRE_OUTAGE_MARGIN * backup_kw_on_phase * cut_hours
    return min(PRE_OUTAGE_SOC_CAP, SOC_BACKUP_FLOOR + need_kwh / (block.capacity_kwh * block.efficiency))


def live_decision(
    i: int,
    *,
    load_kw: Optional[float],
    busbar_v_pu: float,
    far_end_v_pu: float,
    soc: float,
    block: BlockSpec,
    dvdp_pu_per_kw: float,
    v_limit_pu: float,
    forecast_p50_kw: np.ndarray,
    bias_kw: float,
    local_hour: np.ndarray,
    pre_outage: bool,
    reserve_soc: float,
) -> tuple[Decision, float]:
    """One block's setpoint for interval i, and the updated forecast bias.

    1. Safe limits from the busbar voltage and its sensitivity to battery
       power, so a battery action never creates a violation of its own.
    2. Pre-outage below the reserve: top up as fast as the limit allows.
    3. Otherwise re-solve the peak-shave / valley-fill plan on what is
       left of the window (planner_shave.live_setpoint), with the floor
       raised to the reserve in Pre-outage.
    4. A far-end overvoltage forces extra charging to absorb the export.

    `load_kw` is the busbar CT reading; None (a dropped uplink) falls back
    to the bias-corrected forecast and leaves the bias unchanged.
    """
    max_dis = float(np.clip((1 + v_limit_pu - busbar_v_pu) / dvdp_pu_per_kw, 0, block.power_kw))
    max_ch = float(np.clip((busbar_v_pu - (1 - v_limit_pu)) / dvdp_pu_per_kw, 0, block.power_kw))
    measured = load_kw if load_kw is not None else float(forecast_p50_kw[i]) + bias_kw
    if pre_outage and soc < reserve_soc:
        sp, rule = -max_ch, "pre_outage_reserve"
    else:
        floor = max(block.soc_min, reserve_soc) if pre_outage else block.soc_min
        sp, rule = live_setpoint(i, measured, forecast_p50_kw, bias_kw, local_hour, soc, block.capacity_kwh,
                                 block.power_kw, floor, block.soc_max, block.efficiency)
        if far_end_v_pu > 1 + v_limit_pu and soc < block.soc_max:
            sp, rule = min(sp, -max_ch), "overvoltage"
    sp = float(np.clip(sp, -max_ch, max_dis))
    new_bias = measured - float(forecast_p50_kw[i]) if load_kw is not None else bias_kw
    mode = "pre_outage" if pre_outage else "normal"
    return Decision(sp, rule, mode, max_ch, max_dis), new_bias


def local_hours(day_ts: pd.DatetimeIndex) -> np.ndarray:
    """IST hour of each interval END, the convention planner_shave uses."""
    return ((day_ts.hour + day_ts.minute / 60.0 + 5.5) % 24).to_numpy()


def day_intervals(plan_date) -> pd.DatetimeIndex:
    """The 96 interval ends of a UTC simulation day."""
    return pd.date_range(pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")


@dataclass
class _Notice:
    notice_ts: datetime
    start: datetime
    end: datetime
    reason: str
    reserve_soc: dict


@dataclass
class _Outage:
    start: datetime
    scope: str
    cause: str
    planned: bool
    phases: list
    expected_end: datetime


@dataclass
class TickOutput:
    events: list = field(default_factory=list)          # [EventRecord]
    transitions: list = field(default_factory=list)     # [ModeTransition]
    premise_limits_a: Optional[dict] = None             # {household_id: amps}; None = unchanged


class Controller:
    """Stateful C7 + C9 for one distribution transformer.

    `sensors`: id, phase, placement ('busbar' | 'far_end'), bus_id.
    `premises`: household_id, phase, priority_class, max_current_a.
    `dvdp`: per-phase busbar voltage sensitivity to battery power (pu/kW),
    from the gateway's own network model (C6).
    """

    def __init__(self, blocks: dict[str, BlockSpec], sensors: pd.DataFrame, premises: pd.DataFrame,
                 nominal_v_ln: float, v_limit_pct: float, dvdp: dict[str, float]):
        self.blocks = blocks
        self.sensors = sensors.reset_index(drop=True)
        self.premises = premises.reset_index(drop=True)
        self.nominal_v_ln = nominal_v_ln
        self.v_limit_pu = v_limit_pct / 100.0
        self.dvdp = {p: max(v, 1e-6) for p, v in dvdp.items()}
        self.mode = ModeState(mode="normal")
        self.supply = {sid: True for sid in self.sensors["id"]}
        self.notice: Optional[_Notice] = None
        self.outage: Optional[_Outage] = None
        self.bias = {p: 0.0 for p in PHASES}
        self.day_ts: Optional[pd.DatetimeIndex] = None
        self.local_hour: Optional[np.ndarray] = None
        self.forecast_p50: dict[str, np.ndarray] = {}

    # ---- day-ahead inputs -------------------------------------------------

    def start_day(self, plan_date, forecast_p50: dict[str, np.ndarray]) -> None:
        self.day_ts = day_intervals(plan_date)
        self.local_hour = local_hours(self.day_ts)
        self.forecast_p50 = {p: np.asarray(v, dtype=float) for p, v in forecast_p50.items()}
        self.bias = {p: 0.0 for p in PHASES}

    def interval_index(self, interval_start: datetime) -> Optional[int]:
        """Index of the interval that starts at `interval_start`, or None
        if it isn't a boundary of the planned day."""
        end = pd.Timestamp(interval_start) + INTERVAL
        hits = np.where(self.day_ts == end)[0] if self.day_ts is not None else []
        return int(hits[0]) if len(hits) else None

    # ---- C7: Pre-outage on a DISCOM notice -------------------------------

    def announce_load_shedding(self, notice_ts: datetime, start: datetime, end: datetime,
                               reason: str = "") -> TickOutput:
        cut_h = (end - start).total_seconds() / 3600.0
        reserve = {}
        for p, block in self.blocks.items():
            kw = float((self.premises.loc[self.premises["phase"] == p, "max_current_a"] * self.nominal_v_ln / 1000.0).sum())
            reserve[p] = pre_outage_reserve_soc(block, kw, cut_h)
        self.notice = _Notice(notice_ts, start, end, reason, reserve)
        out = TickOutput(events=[EventRecord(notice_ts, "load_shedding_notice", "upstream", {
            "scheduled_start": start.isoformat(), "scheduled_end": end.isoformat(), "reason": reason,
            "reserve_soc": reserve}, source="discom")])
        if self.mode.mode == "normal":
            self.mode, tr = transition(self.mode, notice_ts, pre_outage_risk=True)
            tr.reason = "DISCOM load-shedding notice"
            out.transitions.append(tr)
        return out

    # ---- C9: sensor events ------------------------------------------------

    def set_supply(self, sensor_id: str, present: bool) -> None:
        if sensor_id in self.supply:
            self.supply[sensor_id] = present

    def islanded_phases(self) -> list[str]:
        return list(self.outage.phases) if self.outage else []

    def _phases_for_scope(self, scope: str) -> list[str]:
        if scope == "upstream":
            return list(PHASES)
        if scope.startswith("phase:"):
            return [scope.split(":", 1)[1]]
        return []  # a local fault downstream of the busbar: the block still sees the grid

    def _allocate(self, ts: datetime, soc: dict[str, float]):
        remaining_h = max(DT_H, (self.outage.expected_end - ts).total_seconds() / 3600.0)
        available_kwh, available_kw = {}, {}
        for p in PHASES:
            block = self.blocks[p]
            if p in self.outage.phases:
                available_kwh[p] = max(0.0, soc[p] - SOC_BACKUP_FLOOR) * block.capacity_kwh * block.efficiency
                available_kw[p] = min(block.power_kw, available_kwh[p] / remaining_h)
            else:
                available_kwh[p], available_kw[p] = 0.0, 0.0
        phase_by_hh = dict(zip(self.premises["household_id"], self.premises["phase"]))
        allocations = allocate_backup_power(self.premises[["household_id", "priority_class", "max_current_a"]],
                                            phase_by_hh, available_kw, self.nominal_v_ln)
        limits = {a.household_id: (a.allocated_kw * 1000.0 / self.nominal_v_ln if a.phase in self.outage.phases else 0.0)
                  for a in allocations}
        return allocations, limits, available_kwh

    def on_tick(self, ts: datetime, soc: dict[str, float]) -> TickOutput:
        """C9 at every tick: detect grid loss / return from the sensor
        picture, run Backup allocation, stage Restoration, and clear a
        Pre-outage whose cut window passed without one."""
        out = TickOutput()
        picture = classify_outage(self.sensors, self.supply)

        if self.outage is None and picture is not None:
            cls, scope = picture
            phases = self._phases_for_scope(scope)
            if phases:
                n = self.notice
                planned = n is not None and n.start - INTERVAL <= ts <= n.end
                cause = "load_shedding" if planned else CAUSE_BY_CLASS[cls]
                expected_end = n.end if planned else ts + UNPLANNED_EXPECTED
                self.outage = _Outage(ts, scope, cause, planned, phases, expected_end)
                self.mode, tr = transition(self.mode, ts, grid_lost=True)
                tr.reason = f"grid lost ({cause.replace('_', ' ')}, {scope})"
                out.transitions.append(tr)
                down = sorted(s for s, up in self.supply.items() if not up)
                out.events.append(EventRecord(ts, "grid_loss", scope, {"cause": cause, "detected_by": down,
                                                                       "expected_end": expected_end.isoformat()}))
                allocations, limits, available_kwh = self._allocate(ts, soc)
                served = [a for a in allocations if a.phase in phases]
                out.events.append(EventRecord(ts, "backup_start", scope, {
                    "n_premises": len(served), "total_kw": sum(a.allocated_kw for a in served),
                    "premises": [{"household_id": a.household_id, "phase": a.phase, "allocated_kw": a.allocated_kw,
                                  "max_kw": a.max_kw, "priority_class": a.priority_class} for a in served],
                    "soc_at_loss": {p: soc[p] for p in phases},
                    "available_kwh_by_phase": {p: available_kwh[p] for p in phases},
                }))
                out.premise_limits_a = limits

        elif self.outage is not None and picture is None:
            out.events.append(EventRecord(ts, "grid_return", self.outage.scope, {"cause": self.outage.cause}))
            self.mode, tr = transition(self.mode, ts, grid_returned=True)
            out.transitions.append(tr)
            served = self.premises[self.premises["phase"].isin(self.outage.phases)]
            batches = stage_restoration(served[["household_id", "priority_class", "max_current_a"]])
            for k, batch in enumerate(batches):
                out.events.append(EventRecord(ts + k * RESTORATION_BATCH_GAP, "restore", self.outage.scope,
                                              {"batch": k, "household_ids": batch}))
            done = ts + max(1, len(batches)) * RESTORATION_BATCH_GAP
            out.events.append(EventRecord(done, "backup_end", self.outage.scope, {}))
            self.mode, tr = transition(self.mode, done, restoration_complete=True)
            out.transitions.append(tr)
            out.premise_limits_a = {h: 0.0 for h in self.premises["household_id"]}
            self.outage = None
            if self.notice is not None and ts >= self.notice.start:
                self.notice = None

        elif self.outage is not None:
            if not self.outage.planned and ts >= self.outage.expected_end:
                self.outage.expected_end = ts + UNPLANNED_EXTENSION
            _, out.premise_limits_a, _ = self._allocate(ts, soc)

        if self.outage is None and self.notice is not None and ts > self.notice.end:
            if self.mode.mode == "pre_outage":
                self.mode, tr = transition(self.mode, ts, risk_cleared=True)
                tr.reason = "announced cut window passed"
                out.transitions.append(tr)
            self.notice = None
        return out

    # ---- C7: live correction at an interval boundary ---------------------

    def decide(self, i: int, *, load_kw: dict, busbar_v_pu: dict, far_end_v_pu: dict,
               soc: dict) -> dict[str, Decision]:
        pre_outage = self.mode.mode == "pre_outage"
        islanded = set(self.islanded_phases())
        decisions = {}
        for p in PHASES:
            block = self.blocks[p]
            if p in islanded:
                # The inverter has anti-islanded onto its backup port (IEC 62116);
                # its grid port is not LEO's to command until the grid returns.
                decisions[p] = Decision(0.0, None, "backup")
                continue
            reserve = self.notice.reserve_soc[p] if (pre_outage and self.notice) else 0.0
            decisions[p], self.bias[p] = live_decision(
                i, load_kw=load_kw.get(p), busbar_v_pu=busbar_v_pu[p], far_end_v_pu=far_end_v_pu[p],
                soc=soc[p], block=block, dvdp_pu_per_kw=self.dvdp[p], v_limit_pu=self.v_limit_pu,
                forecast_p50_kw=self.forecast_p50[p], bias_kw=self.bias[p], local_hour=self.local_hour,
                pre_outage=pre_outage, reserve_soc=reserve)
        return decisions


__all__ = [
    "BlockSpec", "Controller", "Decision", "EventRecord", "ModeTransition", "TickOutput", "day_intervals",
    "live_decision", "local_hours", "pre_outage_reserve_soc",
]
