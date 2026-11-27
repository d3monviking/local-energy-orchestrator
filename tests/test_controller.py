"""gateway/controller.py: C7 live correction and C9 outage handling."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from gateway.controller import (
    PHASES, SOC_BACKUP_FLOOR, BlockSpec, Controller, day_intervals, live_decision, local_hours,
    pre_outage_reserve_soc,
)

DAY = datetime(2026, 4, 27, tzinfo=timezone.utc)
BLOCK = BlockSpec("R", 60.0, 15.0)
DVDP = 0.002  # pu per kW


def sensors() -> pd.DataFrame:
    rows = []
    for p in PHASES:
        rows.append({"id": f"SEN-BUSBAR-{p}", "bus_id": "BUS-0", "phase": p, "placement": "busbar", "dev_eui": f"b{p}"})
        rows.append({"id": f"SEN-FAREND-{p}", "bus_id": f"BUS-{p}", "phase": p, "placement": "far_end", "dev_eui": f"f{p}"})
    return pd.DataFrame(rows)


def premises() -> pd.DataFrame:
    return pd.DataFrame([
        {"household_id": "HH-014", "phase": "B", "priority_class": 3, "max_current_a": 6.0},
        {"household_id": "HH-056", "phase": "R", "priority_class": 1, "max_current_a": 16.0},
        {"household_id": "HH-069", "phase": "R", "priority_class": 3, "max_current_a": 6.0},
        {"household_id": "HH-136", "phase": "Y", "priority_class": 2, "max_current_a": 10.0},
    ])


def controller(soc_block=BLOCK) -> Controller:
    blocks = {p: BlockSpec(p, soc_block.capacity_kwh, soc_block.power_kw) for p in PHASES}
    c = Controller(blocks, sensors(), premises(), 250.0, 6.0, {p: DVDP for p in PHASES})
    c.start_day(DAY.date(), {p: np.full(96, 40.0) for p in PHASES})
    return c


def decide(i, **kw):
    ts = day_intervals(DAY.date())
    args = dict(load_kw=40.0, busbar_v_pu=1.0, far_end_v_pu=0.97, soc=0.5, block=BLOCK, dvdp_pu_per_kw=DVDP,
                v_limit_pu=0.06, forecast_p50_kw=np.full(96, 40.0), bias_kw=0.0, local_hour=local_hours(ts),
                pre_outage=False, reserve_soc=0.0)
    args.update(kw)
    return live_decision(i, **args)


# ---- C7 -----------------------------------------------------------------

def test_evening_discharge_is_peak_shave_within_voltage_limit():
    i = 52  # 13:00 UTC = 18:30 IST, inside the discharge window
    d, bias = decide(i, load_kw=55.0)
    assert d.rule == "peak_shave" and 0 < d.setpoint_kw <= d.max_discharge_kw
    assert bias == pytest.approx(15.0)  # measured 55 vs forecast 40


def test_busbar_at_upper_limit_forbids_discharge():
    d, _ = decide(52, load_kw=55.0, busbar_v_pu=1.06)
    assert d.max_discharge_kw == 0.0 and d.setpoint_kw == 0.0


def test_missing_ct_reading_falls_back_to_forecast_and_keeps_bias():
    d, bias = decide(52, load_kw=None, bias_kw=3.0)
    assert bias == 3.0
    assert d.rule == "peak_shave"


def test_pre_outage_below_reserve_charges_at_the_safe_limit():
    d, _ = decide(52, pre_outage=True, reserve_soc=0.6, soc=0.4)
    assert d.rule == "pre_outage_reserve" and d.mode == "pre_outage"
    assert d.setpoint_kw == pytest.approx(-d.max_charge_kw)


def test_far_end_overvoltage_forces_charging():
    d, _ = decide(40, load_kw=-5.0, far_end_v_pu=1.08)  # midday export
    assert d.rule == "overvoltage" and d.setpoint_kw < 0


def test_pre_outage_reserve_formula():
    # 1.5 x 4 kW x 1.5 h = 9 kWh above the 5% floor of a 60 kWh / 92% block
    assert pre_outage_reserve_soc(BLOCK, 4.0, 1.5) == pytest.approx(0.05 + 9.0 / (60 * 0.92))
    assert pre_outage_reserve_soc(BlockSpec("R", 5.0, 2.5), 10.0, 3.0) == 0.90  # capped


# ---- C9 -----------------------------------------------------------------

def _all_supply(c: Controller, present: bool, phases=PHASES):
    for s in sensors().itertuples():
        if s.phase in phases:
            c.set_supply(s.id, present)


def test_load_shedding_notice_enters_pre_outage_then_planned_backup_and_restoration():
    c = controller()
    notice = DAY + timedelta(hours=8, minutes=30)
    start, end = DAY + timedelta(hours=14, minutes=10), DAY + timedelta(hours=15, minutes=40)
    out = c.announce_load_shedding(notice, start, end, "short of power")
    assert c.mode.mode == "pre_outage"
    assert [t.to_mode for t in out.transitions] == ["pre_outage"]
    assert out.events[0].kind == "load_shedding_notice" and out.events[0].source == "discom"
    assert all(0.05 < r < 0.9 for r in c.notice.reserve_soc.values())

    _all_supply(c, False)
    out = c.on_tick(start, {p: 0.8 for p in PHASES})
    assert c.mode.mode == "backup" and c.islanded_phases() == list(PHASES)
    kinds = [e.kind for e in out.events]
    assert kinds == ["grid_loss", "backup_start"]
    assert out.events[0].payload["cause"] == "load_shedding"
    # Planned: energy spread over the announced 90 minutes; clinic gets its full 16 A.
    assert out.premise_limits_a["HH-056"] == pytest.approx(16.0)

    decisions = c.decide(c.interval_index(start - timedelta(minutes=10)), load_kw={}, busbar_v_pu={p: 1.0 for p in PHASES},
                         far_end_v_pu={p: 1.0 for p in PHASES}, soc={p: 0.8 for p in PHASES})
    assert all(d.mode == "backup" and d.setpoint_kw == 0.0 for d in decisions.values())

    _all_supply(c, True)
    out = c.on_tick(end, {p: 0.7 for p in PHASES})
    kinds = [e.kind for e in out.events]
    assert kinds[0] == "grid_return" and kinds[-1] == "backup_end" and "restore" in kinds
    assert [t.to_mode for t in out.transitions] == ["restoration", "normal"]
    assert c.mode.mode == "normal" and c.notice is None
    assert all(v == 0.0 for v in out.premise_limits_a.values())


def test_backup_allocates_by_priority_when_energy_is_short():
    c = controller()
    _all_supply(c, False)
    # Phase R barely above the backup floor: the clinic (priority 1) is served first.
    soc = {"R": SOC_BACKUP_FLOOR + 0.01, "Y": 0.8, "B": 0.8}
    out = c.on_tick(DAY + timedelta(hours=14), soc)
    limits = out.premise_limits_a
    assert out.events[0].payload["cause"] == "upstream_fault"
    assert limits["HH-056"] > 0 and limits["HH-069"] == 0.0
    assert limits["HH-056"] < 16.0


def test_unplanned_outage_estimate_is_pushed_out_when_exceeded():
    c = controller()
    _all_supply(c, False)
    t0 = DAY + timedelta(hours=14)
    c.on_tick(t0, {p: 0.8 for p in PHASES})
    first = c.outage.expected_end
    c.on_tick(first + timedelta(minutes=1), {p: 0.3 for p in PHASES})
    assert c.outage.expected_end > first


def test_single_far_end_dark_is_local_and_does_not_island():
    c = controller()
    c.set_supply("SEN-FAREND-R", False)
    out = c.on_tick(DAY + timedelta(hours=14), {p: 0.5 for p in PHASES})
    assert c.mode.mode == "normal" and not out.events and c.islanded_phases() == []


def test_phase_fault_islands_only_that_phase():
    c = controller()
    _all_supply(c, False, phases=("Y",))
    c.on_tick(DAY + timedelta(hours=14), {p: 0.5 for p in PHASES})
    assert c.islanded_phases() == ["Y"]
    d = c.decide(56, load_kw={p: 40.0 for p in PHASES}, busbar_v_pu={p: 1.0 for p in PHASES},
                 far_end_v_pu={p: 0.97 for p in PHASES}, soc={p: 0.5 for p in PHASES})
    assert d["Y"].mode == "backup" and d["R"].mode == "normal"


def test_notice_window_passing_without_a_cut_clears_pre_outage():
    c = controller()
    start, end = DAY + timedelta(hours=14), DAY + timedelta(hours=15)
    c.announce_load_shedding(DAY + timedelta(hours=8), start, end)
    out = c.on_tick(end + timedelta(minutes=15), {p: 0.8 for p in PHASES})
    assert c.mode.mode == "normal" and out.transitions[0].reason == "announced cut window passed"
