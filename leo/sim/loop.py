"""sim/loop.py — per-step closed loop, the real recorded-run producer.
Owner B. Build Specification v1.0 §4.1, §7.2, §7.4.

Distinct from eval/run_arms.py (Owner A's fast, numpy-only, fixed-
schedule two-arm sweep for the M1-M5 metrics over many days): this file
drives the ACTUAL architecture for one day — gateway/forecast/run.py's
real day-ahead plan, cloud/dr_engine's real LinUCB-selected DR offers
sent through mocks/sms_service, sim/personas.py's response model, and
real Modbus TCP setpoints to mocks/modbus_battery.py — and writes every
result straight to Postgres under a `run_id`. Per the locked demo
decision (leo-implementation-plan.md §1): "Pipeline runs beforehand, UI
reads recorded timelines from Postgres. All models execute for real."
That is exactly what this file does; it is not a shortcut.

Per-step order (§7.2):
  1. apply DR responses from personas for any active offer
  2. apply the battery setpoints LEO wrote to units 1-3 (skipped for the
     baseline arm)
  3. run pandapower runpp_3ph
  4. check sustained phase overload; trip the fuse if exceeded
  5. log truth, produce measurements (sim/measure.py)

Usage:
    python -m sim.loop --run-id normal   --leo-enabled
    python -m sim.loop --run-id baseline --no-leo-enabled
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pandapower as pp
import psycopg2
import psycopg2.extras
import requests
from pymodbus.client import AsyncModbusTcpClient
import asyncio

from world.feeder import build_feeder, load_scenario
from world.households import assign_ownership_and_truth, generate_true_load
from world.pv_truth import assign_pv_truth_params, generate_true_pv
from world.weather import fetch_scenario_weather, to_15min
from gateway.network_model import (
    build_pandapower_net, update_household_loads, run_power_flow, detect_violations,
    compute_phase_limits, linear_phase_limits, get_bus_index,
)
from gateway.forecast.load_model import build_features, FEATURE_COLUMNS, train as train_load_model
from gateway.forecast.pv_model import unit_pv_output_kw, fit_k, compute_clear_midday_mask, pass0_initial_load_estimate
from gateway.forecast.run import ForecastInputs, run_day_ahead_plan, approve_plan
from gateway.orchestrator.planner_shave import live_setpoint
from cloud.dr_engine.linucb import LinUCB, choose_level, build_feature_vector, LEVELS
from cloud.dr_engine.selection import run_dr_event, ensure_default_consent
from sim.personas import respond_to_offer
from sim import measure

PHASES = ["R", "Y", "B"]
PHASE_LETTER = {"R": "a", "Y": "b", "B": "c"}
UNIT_ID_BY_PHASE = {"R": 1, "Y": 2, "B": 3}

IST_OFFSET_HOURS = 5.5
TRIP_THRESHOLD_PCT = 100.0
TRIP_CONSECUTIVE_INTERVALS = 4
TRIP_REPAIR_INTERVALS = 8

DR_WINDOW_IST = (19, 21)  # storyboard beat 2: "Phase R goes amber then red at 19:15"
V_RUPEES_PER_KWH = 7.92  # IEX evening peak premium (research note); energy value of a kWh cut
MODBUS_WATCHDOG_S = 120  # comfortably longer than one interval's real-time Modbus round trip


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"),
        password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def _clear_existing_run(conn, run_id: str) -> None:
    cur = conn.cursor()
    for table in [
        "dispatch", "network_result", "phase_limit", "plan", "mode_transition", "forecast",
        "dr_offer", "dr_event", "recommendation", "ledger", "period_revenue",
        "sensor_reading", "meter_interval", "battery_telemetry", "premise_meter", "event",
    ]:
        cur.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,))
    cur.execute("DELETE FROM run WHERE run_id = %s", (run_id,))
    conn.commit()
    cur.close()


def _ist_hour(ts: pd.Timestamp) -> float:
    return (ts.hour + ts.minute / 60.0 + IST_OFFSET_HOURS) % 24


def to_unsigned16(value: int) -> int:
    return value & 0xFFFF


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


@dataclass
class ArmState:
    trip_until: dict = field(default_factory=lambda: {"R": None, "Y": None, "B": None})
    consecutive_overload: dict = field(default_factory=lambda: {"R": 0, "Y": 0, "B": 0})
    soc: dict = field(default_factory=lambda: {"R": 0.5, "Y": 0.5, "B": 0.5})


SOC_MIN, SOC_MAX, BATTERY_EFFICIENCY = 0.15, 0.90, 0.92
PRE_OUTAGE_SOC_TARGET = 0.90  # §9.3: Pre-outage raises the reserve floor on all three blocks (cap)
PRE_OUTAGE_CUT_HOURS = 1.5     # the announced cut's length, from the DISCOM's notice


def bms_step(soc: float, commanded_kw: float, capacity_kwh: float, hours: float = 0.25) -> tuple[float, float]:
    """Energy accounting for one recorded 15-minute interval: clip the
    command so SoC never crosses its bounds, then integrate. Done here, at
    exactly the recorded interval length, because the Modbus mock
    integrates against the clock service's free-running sim time instead
    (confirmed: a full 7.5 kW discharge interval moved its SoC ~0.004
    instead of ~0.125, so the batteries effectively never drained). The
    Modbus round trip is still made; this is just the source of truth for
    how much energy actually moved. Returns (actual_kw, new_soc)."""
    if commanded_kw > 0:
        max_kw = (soc - SOC_MIN) * capacity_kwh * BATTERY_EFFICIENCY / hours
        actual = min(commanded_kw, max(0.0, max_kw))
        return actual, soc - actual * hours / BATTERY_EFFICIENCY / capacity_kwh
    if commanded_kw < 0:
        max_kw = (SOC_MAX - soc) * capacity_kwh / BATTERY_EFFICIENCY / hours
        actual = -min(-commanded_kw, max(0.0, max_kw))
        return actual, soc - actual * hours * BATTERY_EFFICIENCY / capacity_kwh
    return 0.0, soc


async def _write_battery_setpoint(client: AsyncModbusTcpClient, unit_id: int, setpoint_kw: float) -> None:
    await client.write_register(address=40012, value=MODBUS_WATCHDOG_S, device_id=unit_id)
    await client.write_register(address=40010, value=1, device_id=unit_id)  # mode: follow_setpoint
    await client.write_register(address=40011, value=to_unsigned16(round(setpoint_kw * 1000)), device_id=unit_id)


async def _read_battery_telemetry(client: AsyncModbusTcpClient, unit_id: int) -> dict:
    rr = await client.read_holding_registers(address=40000, count=8, device_id=unit_id)
    regs = rr.registers
    return {
        "soc": regs[0] / 1000.0,
        "actual_kw": to_signed16(regs[1]) / 1000.0,
        "terminal_voltage_v": regs[2] / 10.0,
        "grid_present": bool(regs[3]),
        "alarm_bits": regs[4],
    }


def _dispatch_battery_sync(unit_id: int, setpoint_kw: float, modbus_host: str, modbus_port: int) -> dict:
    """One Modbus round trip per call, run from sync code via a fresh
    event loop — sim/loop.py's own control flow is synchronous (it shares
    patterns with eval/run_arms.py and gateway/forecast/run.py, both
    sync), and 96 intervals x 3 phases of short-lived async calls is
    simpler to reason about than threading the whole file through asyncio.
    """
    async def _do():
        client = AsyncModbusTcpClient(modbus_host, port=modbus_port)
        await client.connect()
        try:
            await _write_battery_setpoint(client, unit_id, setpoint_kw)
            # SoC comes from bms_step(), not this read, so don't wait 1.3 s for
            # the mock's physics tick (that alone was ~6 min per recorded day).
            await asyncio.sleep(0.05)
            telemetry = await _read_battery_telemetry(client, unit_id)
        finally:
            client.close()
        return telemetry

    return asyncio.run(_do())


def init_run(conn, run_id: str, leo_enabled: bool, seed: int, plan_date: date_cls, notes: str) -> None:
    """Creates (or re-creates) the `run` row every other table's run_id
    foreign key depends on. Must happen before ANYTHING else writes a
    row under this run_id — including cloud/dr_engine.run_dr_event(),
    which is called before run_one_day() in main() below.
    """
    sim_start = pd.Timestamp(plan_date, tz="UTC")
    sim_end = sim_start + pd.Timedelta(days=1)
    _clear_existing_run(conn, run_id)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO run (run_id, scenario, leo_enabled, seed, sim_start, sim_end, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (run_id, run_id, leo_enabled, seed, sim_start.to_pydatetime(), sim_end.to_pydatetime(), notes),
    )
    conn.commit()
    cur.close()


def run_one_day(
    conn,
    run_id: str,
    leo_enabled: bool,
    plan_date: date_cls,
    scenario: dict,
    feeder,
    net,
    household_load_full_kw: pd.DataFrame,
    approved_plans: dict | None,
    accepted_reduction_kw: dict[str, float],
    v_limit_pct: float,
    nominal_v_ln: float,
    modbus_host: str,
    modbus_port: int,
    rng: np.random.Generator,
    dr_window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    pre_outage: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> None:
    """`pre_outage` = (notice_ts, outage_start): from the DISCOM's load-
    shedding notice until the outage, every block holds charge and tops up
    toward PRE_OUTAGE_SOC_TARGET instead of following the plan (§9.3)."""
    sim_start = pd.Timestamp(plan_date, tz="UTC")
    target_ts = pd.date_range(sim_start + pd.Timedelta(minutes=15), periods=96, freq="15min")
    capacity_by_phase = {b["phase"]: b["capacity_kwh"] for b in scenario["battery_blocks"]}

    cur = conn.cursor()
    if leo_enabled and approved_plans is not None:
        for phase, plan in approved_plans.items():
            for iv in plan["intervals"]:
                cur.execute(
                    """INSERT INTO plan (run_id, plan_date, phase, ts_end, setpoint_kw, mode,
                           planner, reserve_kwh, approved_at, approved_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id, plan_date, phase, iv["ts_end"], iv["setpoint_kw"], iv["mode"],
                     plan["planner"], plan["reserve_floor_kwh"], plan["approved_at"], plan["approved_by"]),
                )
        conn.commit()

    bus_id_by_hhid = dict(zip(feeder.household["id"], feeder.household["bus_id"]))
    far_end_sensor_bus = {
        row["phase"]: row["bus_id"]
        for _, row in feeder.sensor[feeder.sensor["placement"] == "far_end"].iterrows()
    }

    state = ArmState()
    root_idx = get_bus_index(net, feeder.root)
    far_end_idx = {p: get_bus_index(net, b) for p, b in far_end_sensor_bus.items()}
    bus_by_phase = {p: feeder.household.loc[feeder.household["phase"] == p, "bus_id"].tolist() for p in PHASES}
    local_hour = ((target_ts.hour + target_ts.minute / 60.0 + IST_OFFSET_HOURS) % 24).to_numpy()
    dvdp: dict[str, float] = {}
    bias = {p: 0.0 for p in PHASES}
    reserve_soc = {p: SOC_MIN for p in PHASES}
    if pre_outage is not None:
        from gateway.outage import register_backup_premises
        backup = register_backup_premises(feeder.household)
        backup["phase"] = backup["household_id"].map(dict(zip(feeder.household["id"], feeder.household["phase"])))
        cut_h = PRE_OUTAGE_CUT_HOURS
        for p in PHASES:
            need_kwh = 1.5 * float((backup.loc[backup["phase"] == p, "max_current_a"] * nominal_v_ln / 1000.0).sum()) * cut_h
            cap = next(b["capacity_kwh"] for b in scenario["battery_blocks"] if b["phase"] == p)
            reserve_soc[p] = min(PRE_OUTAGE_SOC_TARGET, max(SOC_MIN, 0.05 + need_kwh / (cap * BATTERY_EFFICIENCY)))

    n_trips = 0
    for i, ts in enumerate(target_ts):
        loads = household_load_full_kw.loc[ts].to_dict() if ts in household_load_full_kw.index else {}

        for phase in PHASES:
            if state.trip_until[phase] is not None and ts < state.trip_until[phase]:
                for bus_id in feeder.household.loc[feeder.household["phase"] == phase, "bus_id"]:
                    loads[bus_id] = 0.0
            elif state.trip_until[phase] is not None and ts >= state.trip_until[phase]:
                state.trip_until[phase] = None
                state.consecutive_overload[phase] = 0

        # Only inside the DR window. This used to apply all 96 intervals,
        # giving the LEO run a full day of demand reduction from a 2-hour
        # event (write_meter_intervals already checked the window, so the
        # meters and the power flow disagreed).
        in_dr_window = dr_window is not None and dr_window[0] <= ts < dr_window[1]
        if leo_enabled and accepted_reduction_kw and in_dr_window:
            for hh_id, reduction_kw in accepted_reduction_kw.items():
                bus_id = bus_id_by_hhid.get(hh_id)
                if bus_id in loads:
                    loads[bus_id] = loads[bus_id] - reduction_kw

        update_household_loads(net, feeder, loads)

        sgen_indices = []
        dispatch_rows = []
        if leo_enabled and approved_plans is not None:
            # One pre-dispatch solve stands in for the sensors' latest reading
            # (busbar CT + voltage, far-end voltage) for all three phases.
            run_power_flow(net)
            res = net.res_bus_3ph
            for phase in PHASES:
                if state.trip_until[phase] is not None:
                    continue
                plan = approved_plans[phase]
                planned_kw = plan["intervals"][i]["setpoint_kw"]
                letter = PHASE_LETTER[phase]
                block = next(b for b in scenario["battery_blocks"] if b["phase"] == phase)
                cap = block["capacity_kwh"]
                if phase not in dvdp:
                    dvdp[phase] = compute_phase_limits(net, feeder.root, phase, v_limit_pct, block["power_kw"]).dv_dp_pu_per_kw
                limits = linear_phase_limits(net, root_idx, phase, v_limit_pct, block["power_kw"], dvdp[phase])
                far_end_v = float(res.at[far_end_idx[phase], f"vm_{letter}_pu"]) * nominal_v_ln
                measured_kw = float(sum(loads.get(b, 0.0) for b in bus_by_phase[phase]))
                forecast_p50 = np.asarray(plan["forecast_p50_kw"])

                mode = "normal"
                if pre_outage is not None and pre_outage[0] <= ts < pre_outage[1]:
                    # §9.3 Pre-outage: raise the floor to what the backup
                    # premises need through the announced cut (+50%), top
                    # up to it, and keep shaving the peak with the rest.
                    mode = "pre_outage"
                    floor = reserve_soc[phase]
                    if state.soc[phase] < floor:
                        setpoint_kw, rule = -limits.max_charge_kw, "pre_outage_reserve"
                    else:
                        setpoint_kw, rule = live_setpoint(i, measured_kw, forecast_p50, bias[phase], local_hour,
                                                          state.soc[phase], cap, block["power_kw"], floor, SOC_MAX,
                                                          BATTERY_EFFICIENCY)
                else:
                    setpoint_kw, rule = live_setpoint(i, measured_kw, forecast_p50, bias[phase], local_hour,
                                                      state.soc[phase], cap, block["power_kw"], SOC_MIN, SOC_MAX,
                                                      BATTERY_EFFICIENCY)
                    upper = nominal_v_ln * (1 + v_limit_pct / 100.0)
                    if far_end_v > upper and state.soc[phase] < SOC_MAX:
                        setpoint_kw, rule = min(setpoint_kw, -limits.max_charge_kw), "overvoltage"
                setpoint_kw = float(np.clip(setpoint_kw, -limits.max_charge_kw, limits.max_discharge_kw))
                bias[phase] = measured_kw - float(forecast_p50[i])

                # Real Modbus write (transport + watchdog fidelity) ...
                _dispatch_battery_sync(UNIT_ID_BY_PHASE[phase], setpoint_kw, modbus_host, modbus_port)
                # ... energy accounting at the recorded interval length.
                actual_kw, state.soc[phase] = bms_step(state.soc[phase], setpoint_kw, cap)

                if actual_kw != 0:
                    idx = pp.create_asymmetric_sgen(net, bus=root_idx, **{f"p_{letter}_mw": actual_kw / 1000.0})
                    sgen_indices.append(idx)

                dispatch_rows.append((phase, planned_kw, actual_kw, state.soc[phase], rule, mode))

        run_power_flow(net)
        violations = detect_violations(net, v_limit_pct)

        for phase in PHASES:
            phase_v = violations[violations["phase"] == phase]
            # Fuse trip is on conductor (line) loading only. The busbar row
            # now carries the TRANSFORMER's loading, which runs 150%+ every
            # evening on this day: Indian DTs routinely ride that for hours
            # (thermal time constant, HRC fuses sized above rating) and fail
            # by ageing, not an instant trip. It's reported as an overload
            # event, not modelled as a trip.
            line_v = phase_v[phase_v["bus_id"] != str(feeder.root)]
            max_loading = line_v["loading_pct"].max() if not line_v.empty and line_v["loading_pct"].notna().any() else 0.0
            if max_loading is None or (isinstance(max_loading, float) and np.isnan(max_loading)):
                max_loading = 0.0

            if max_loading > TRIP_THRESHOLD_PCT and state.trip_until[phase] is None:
                state.consecutive_overload[phase] += 1
                if state.consecutive_overload[phase] >= TRIP_CONSECUTIVE_INTERVALS:
                    state.trip_until[phase] = ts + pd.Timedelta(minutes=15 * TRIP_REPAIR_INTERVALS)
                    n_trips += 1
                    cur.execute(
                        """INSERT INTO event (run_id, ts, kind, scope, source, payload)
                           VALUES (%s,%s,'overload_trip',%s,'sim',%s)""",
                        (run_id, ts.to_pydatetime(), f"phase:{phase}",
                         psycopg2.extras.Json({"max_loading_pct": float(max_loading)})),
                    )
            else:
                state.consecutive_overload[phase] = 0

            for _, row in phase_v.iterrows():
                cur.execute(
                    """INSERT INTO network_result (run_id, ts_end, bus_id, phase, voltage_v,
                           loading_pct, violation, is_forecast)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)
                       ON CONFLICT (run_id, ts_end, bus_id, phase, is_forecast) DO NOTHING""",
                    (run_id, ts.to_pydatetime(), row["bus_id"], phase, row["voltage_v"],
                     row["loading_pct"], bool(row["violation"])),
                )

        for phase, planned_kw, actual_kw, soc, rule, mode in dispatch_rows:
            block_id = f"BATT-{phase}"
            cur.execute(
                """INSERT INTO dispatch (run_id, block_id, ts_end, setpoint_kw, actual_kw,
                       soc_after, mode, rule_triggered)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, block_id, ts.to_pydatetime(), planned_kw, actual_kw, soc, mode, rule),
            )

        if sgen_indices:
            net.asymmetric_sgen.drop(sgen_indices, inplace=True)

        for _, sensor_row in feeder.sensor.iterrows():
            bus_v_row = violations[(violations["bus_id"] == sensor_row["bus_id"]) & (violations["phase"] == sensor_row["phase"])]
            true_v = float(bus_v_row["voltage_v"].iloc[0]) if not bus_v_row.empty else nominal_v_ln
            supply_present = state.trip_until[sensor_row["phase"]] is None
            reading = measure.sensor_reading(true_v, supply_present, rng)
            if reading is not None:
                cur.execute(
                    """INSERT INTO sensor_reading (run_id, sensor_id, ts_end, voltage_v, supply_present, provenance)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (run_id, sensor_id, ts_end) DO NOTHING""",
                    (run_id, sensor_row["id"], ts.to_pydatetime(), reading["voltage_v"],
                     reading["supply_present"], reading["provenance"]),
                )

        conn.commit()

        if i % 24 == 0:
            print(f"  {run_id}: {ts} done ({i+1}/96), trips so far: {n_trips}")

    cur.close()
    print(f"{run_id}: finished, {n_trips} overload trip(s) over the day")


def write_meter_intervals(
    conn,
    run_id: str,
    feeder,
    true_load: pd.DataFrame,
    true_pv: pd.DataFrame,
    target_ts: pd.DatetimeIndex,
    accepted_reduction_kw: dict[str, float],
    dr_window: tuple[pd.Timestamp, pd.Timestamp] | None,
    interval_hours: float,
    rng: np.random.Generator,
) -> None:
    """Truth (true_load/true_pv, household-keyed) -> the meter_interval
    rows settlement actually reads (sim/measure.py's job), for every
    household and interval of the day. This is what makes Stream 1/2 and
    the per-household baseline in cloud/settlement/*.py (Owner A) have
    real data to run against, rather than requiring a live mock-IES
    round trip sim/loop.py has no need to make for a recorded run.

    A household that accepted a DR offer actually drew less during the
    DR window — not reflected in `true_load` itself, since personas.py's
    response is applied only at settlement/dispatch time (Day 3) — so
    its metered import is reduced here too, for exactly the same reason
    run_one_day() reduces that household's network load: a meter and a
    power flow must agree on what happened.
    """
    cur = conn.cursor()
    pv_hh_ids = set(true_pv.columns)
    for hh_id in feeder.household["id"]:
        load_series = true_load[hh_id] if hh_id in true_load.columns else None
        if load_series is None:
            continue
        pv_series = true_pv[hh_id] if hh_id in pv_hh_ids else None
        reduction_kw = accepted_reduction_kw.get(hh_id, 0.0)

        for ts in target_ts:
            true_kw = float(load_series.get(ts, 0.0))
            if reduction_kw > 0 and dr_window is not None and dr_window[0] <= ts < dr_window[1]:
                true_kw = max(0.0, true_kw - reduction_kw)
            pv_kw = float(pv_series.get(ts, 0.0)) if pv_series is not None else 0.0

            import_kwh = max(0.0, true_kw - pv_kw) * interval_hours
            export_kwh = max(0.0, pv_kw - true_kw) * interval_hours
            true_voltage_v = 230.0  # sim/measure.sensor_reading's job to vary this per sensor; meters report nominal-ish

            row = measure.meter_interval(hh_id, ts.to_pydatetime(), import_kwh, export_kwh, true_voltage_v, rng)
            if row is None:
                continue
            cur.execute(
                """INSERT INTO meter_interval (run_id, household_id, ts_end, import_kwh, export_kwh,
                       avg_voltage_v, received_at, provenance)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, household_id, ts_end) DO NOTHING""",
                (run_id, hh_id, row["ts_end"], row["import_kwh"], row["export_kwh"],
                 row["avg_voltage_v"], row["received_at"], row["provenance"]),
            )
    conn.commit()
    cur.close()


def run_outage_day(
    conn,
    run_id: str,
    plan_date: date_cls,
    scenario: dict,
    feeder,
    outage_start: pd.Timestamp,
    outage_end: pd.Timestamp,
    rng: np.random.Generator,
    source_run_id: str = "normal",
    cause: str = "upstream_fault",
) -> dict:
    """An outage overlaid on a recorded day (Build Spec v1.0 §7.4, §7.6).

    Two shapes:
      - unplanned (`source_run_id` != `run_id`): a fresh run that copies
        everything outside the outage window from the source run, since
        nothing about those intervals differs;
      - planned load shedding (`source_run_id == run_id`): run_one_day has
        already simulated the whole day for this run (including the
        Pre-outage hours); only the outage window is replaced here.

    During the outage there is no grid source for runpp_3ph to solve
    against, so no network_result rows exist for it — the backup circuit
    is a separate, physically disconnected circuit (anti-islanding, §C2).
    Sensors report no supply. Backup power is sized from the battery's
    ACTUAL state of charge when the grid drops (down to a 5% hard floor),
    not a fixed reserve — which is exactly why Pre-outage pre-charging
    matters.
    """
    from gateway.outage import register_backup_premises, allocate_backup_power, stage_restoration
    from gateway.orchestrator.modes import ModeState, transition

    window = (outage_start.to_pydatetime(), outage_end.to_pydatetime())
    cur = conn.cursor()

    if source_run_id != run_id:
        init_run(conn, run_id, True, scenario["sim"]["seed"], plan_date,
                 f"sim/loop.py recorded run: unplanned {cause} {outage_start}–{outage_end}")
        cur.execute(
            """INSERT INTO plan (run_id, plan_date, phase, ts_end, setpoint_kw, mode, planner,
                   reserve_kwh, approved_at, approved_by)
               SELECT %s, plan_date, phase, ts_end, setpoint_kw, mode, planner, reserve_kwh, approved_at, approved_by
               FROM plan WHERE run_id = %s""",
            (run_id, source_run_id),
        )
        for table, cols in [
            ("dispatch", "block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered"),
            ("network_result", "ts_end, bus_id, phase, voltage_v, loading_pct, violation, is_forecast"),
            ("sensor_reading", "sensor_id, ts_end, voltage_v, supply_present, provenance"),
        ]:
            cur.execute(
                f"""INSERT INTO {table} (run_id, {cols}) SELECT %s, {cols} FROM {table}
                    WHERE run_id = %s AND (ts_end <= %s OR ts_end > %s)""",
                (run_id, source_run_id, *window),
            )
        cur.execute(
            """INSERT INTO event (run_id, ts, kind, scope, source, payload)
               SELECT %s, ts, kind, scope, source, payload FROM event
               WHERE run_id = %s AND kind IN ('dr_event_start','dr_event_end')""",
            (run_id, source_run_id),
        )
        copy_dr_rows(conn, source_run_id, run_id)
    else:
        # run_one_day simulated these intervals as if the grid were up.
        for table in ("dispatch", "network_result", "sensor_reading"):
            cur.execute(f"DELETE FROM {table} WHERE run_id = %s AND ts_end > %s AND ts_end <= %s", (run_id, *window))
    conn.commit()

    # State of charge on each block at the moment the grid drops.
    cur.execute(
        """SELECT DISTINCT ON (block_id) block_id, soc_after FROM dispatch
           WHERE run_id = %s AND ts_end <= %s ORDER BY block_id, ts_end DESC""",
        (run_id, window[0]),
    )
    soc_at_loss = {r[0].replace("BATT-", ""): float(r[1]) for r in cur.fetchall()}

    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'grid_loss','upstream','sim',%s)",
        (run_id, window[0], psycopg2.extras.Json({"cause": cause})),
    )
    for phase in PHASES:
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'power_fail',%s,'sensor',%s)",
            (run_id, window[0], f"phase:{phase}", psycopg2.extras.Json({})),
        )

    # Modes: a load-shedding run is already in pre_outage (set by the notice).
    cur.execute("SELECT to_mode FROM mode_transition WHERE run_id = %s ORDER BY ts DESC LIMIT 1", (run_id,))
    last = cur.fetchone()
    mode_state = ModeState(mode=last[0] if last else "normal")
    mode_state, tr = transition(mode_state, window[0], grid_lost=True)
    cur.execute(
        "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, tr.ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason),
    )

    premise_backup = register_backup_premises(feeder.household)
    household_phase_by_id = dict(zip(feeder.household["id"], feeder.household["phase"]))
    nominal_v_ln = scenario["neighbourhood"]["nominal_v_ln"]
    outage_hours = (outage_end - outage_start).total_seconds() / 3600.0
    capacity = {b["phase"]: b["capacity_kwh"] for b in scenario["battery_blocks"]}
    BACKUP_HARD_FLOOR = 0.05
    available_kwh_by_phase = {
        p: max(0.0, soc_at_loss.get(p, SOC_MIN) - BACKUP_HARD_FLOOR) * capacity[p] for p in capacity
    }
    available_kw_by_phase = {p: e / max(outage_hours, 0.25) for p, e in available_kwh_by_phase.items()}
    allocations = allocate_backup_power(premise_backup, household_phase_by_id, available_kw_by_phase, nominal_v_ln)

    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'backup_start','upstream','sim',%s)",
        (run_id, window[0], psycopg2.extras.Json({
            "n_premises": len(allocations),
            "total_kw": sum(a.allocated_kw for a in allocations),
            "premises": [{"household_id": a.household_id, "phase": a.phase, "allocated_kw": a.allocated_kw,
                          "max_kw": a.max_kw, "priority_class": a.priority_class} for a in allocations],
            "soc_at_loss": soc_at_loss, "available_kwh_by_phase": available_kwh_by_phase,
        })),
    )

    sim_start = pd.Timestamp(plan_date, tz="UTC")
    target_ts = pd.date_range(sim_start + pd.Timedelta(minutes=15), periods=96, freq="15min")
    outage_intervals = [ts for ts in target_ts if outage_start < ts <= outage_end]
    for alloc in allocations:
        current_a = alloc.allocated_kw * 1000.0 / nominal_v_ln
        for ts in outage_intervals:
            cur.execute(
                """INSERT INTO premise_meter (run_id, household_id, ts_end, backup_kwh, current_a, limit_active)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, household_id, ts_end) DO NOTHING""",
                (run_id, alloc.household_id, ts.to_pydatetime(), alloc.allocated_kw * 0.25, current_a,
                 alloc.allocated_kw < alloc.max_kw),
            )
    # Sensors are on the dead grid: they report no supply, not 0 V.
    for _, s in feeder.sensor.iterrows():
        for ts in outage_intervals:
            cur.execute(
                """INSERT INTO sensor_reading (run_id, sensor_id, ts_end, voltage_v, supply_present, provenance)
                   VALUES (%s,%s,%s,NULL,FALSE,'measured') ON CONFLICT DO NOTHING""",
                (run_id, s["id"], ts.to_pydatetime()),
            )
    # Battery blocks serve only their backup port during the outage.
    for p in PHASES:
        soc = soc_at_loss.get(p, SOC_MIN)
        per_interval_kwh = sum(a.allocated_kw for a in allocations if a.phase == p) * 0.25
        for ts in outage_intervals:
            soc = max(BACKUP_HARD_FLOOR, soc - per_interval_kwh / capacity[p])
            cur.execute(
                """INSERT INTO dispatch (run_id, block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered)
                   VALUES (%s,%s,%s,0,0,%s,'backup',NULL)""",
                (run_id, f"BATT-{p}", ts.to_pydatetime(), soc),
            )

    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'grid_return','upstream','sim',%s)",
        (run_id, window[1], psycopg2.extras.Json({})),
    )
    mode_state, tr = transition(mode_state, window[1], grid_returned=True)
    cur.execute(
        "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, tr.ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason),
    )

    batches = stage_restoration(premise_backup)
    batch_gap = timedelta(minutes=3)
    for i, batch in enumerate(batches):
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'restore','upstream','sim',%s)",
            (run_id, window[1] + i * batch_gap, psycopg2.extras.Json({"batch": i, "household_ids": batch})),
        )
    restoration_complete_ts = window[1] + len(batches) * batch_gap
    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'backup_end','upstream','sim',%s)",
        (run_id, restoration_complete_ts, psycopg2.extras.Json({})),
    )
    mode_state, tr = transition(mode_state, restoration_complete_ts, restoration_complete=True)
    cur.execute(
        "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, tr.ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason),
    )

    conn.commit()
    cur.close()
    print(f"{run_id}: {cause} {outage_start}-{outage_end}, SoC at loss {soc_at_loss}, "
          f"{len(allocations)} premises on backup ({sum(a.allocated_kw for a in allocations):.2f} kW), "
          f"{len(batches)} restoration batches")
    return {"n_premises": len(allocations), "allocations": allocations, "batches": batches}


def persist_forecast(conn, run_id: str, artifacts: dict, dt_id: str) -> None:
    """Write what the day-ahead plan was based on (§9.5's C5/C6 outputs):
    P10/P50/P90 net load per phase with its weather inputs -> `forecast`;
    safe charge/discharge limits -> `phase_limit`; predicted per-bus
    voltage and loading -> `network_result` with is_forecast = TRUE.
    These used to be computed and thrown away, which left the operator
    console nothing to explain a plan with."""
    from psycopg2.extras import execute_values

    run_time = artifacts["run_time"].to_pydatetime()
    cur = conn.cursor()
    cur.execute("DELETE FROM forecast WHERE run_id = %s", (run_id,))
    execute_values(cur, """INSERT INTO forecast (run_id, model, model_version, dt_id, phase, ts_end, run_time,
                           p10_kw, p50_kw, p90_kw, inputs_as_of) VALUES %s""", [
        (run_id, "net_load", "lgbm_q+pvlib_v1", dt_id, r["phase"], r["ts_end"].to_pydatetime(), run_time,
         r["p10_kw"], r["p50_kw"], r["p90_kw"],
         psycopg2.extras.Json({"temperature_c": r["temperature_c"], "ghi_w_m2": r["ghi_w_m2"],
                               "issued_at": run_time.isoformat()}))
        for r in artifacts["intervals"]
    ])
    execute_values(cur, """INSERT INTO phase_limit (run_id, ts_end, phase, max_charge_kw, max_discharge_kw, binding_bus_id)
                           VALUES %s ON CONFLICT DO NOTHING""", [
        (run_id, r["ts_end"].to_pydatetime(), r["phase"], r["max_charge_kw"], r["max_discharge_kw"], None)
        for r in artifacts["intervals"]
    ])
    execute_values(cur, """INSERT INTO network_result (run_id, ts_end, bus_id, phase, voltage_v, loading_pct,
                           violation, is_forecast) VALUES %s ON CONFLICT DO NOTHING""", [
        (run_id, r["ts_end"].to_pydatetime(), r["bus_id"], r["phase"], float(r["voltage_v"]),
         None if r["loading_pct"] is None or pd.isna(r["loading_pct"]) else float(r["loading_pct"]),
         r["violation"], True)
        for r in artifacts["predicted"]
    ], page_size=5000)
    conn.commit()
    cur.close()


def copy_dr_rows(conn, src_run: str, dst_run: str) -> None:
    """The DR event is decided the day before, so it's identical across
    runs of the same day — copy rather than re-run the bandit."""
    cur = conn.cursor()
    cur.execute("""INSERT INTO dr_event (run_id, id, phase, window_start, window_end, target_kw, v_paise_kwh)
                   SELECT %s, id, phase, window_start, window_end, target_kw, v_paise_kwh FROM dr_event WHERE run_id = %s
                   ON CONFLICT DO NOTHING""", (dst_run, src_run))
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'dr_offer' AND column_name <> 'run_id'")
    cols = ", ".join(r[0] for r in cur.fetchall())
    cur.execute(f"INSERT INTO dr_offer (run_id, {cols}) SELECT %s, {cols} FROM dr_offer WHERE run_id = %s ON CONFLICT DO NOTHING",
                (dst_run, src_run))
    conn.commit()
    cur.close()


def write_dr_window_events(conn, run_id: str, dr_result: dict, window_start, window_end, n_accepted: int) -> None:
    cur = conn.cursor()
    for kind, ts in (("dr_event_start", window_start), ("dr_event_end", window_end)):
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,%s,%s,'dr_engine',%s)",
            (run_id, ts.to_pydatetime(), kind, f"phase:{dr_result.get('phase', '')}",
             psycopg2.extras.Json({"event_id": dr_result["event_id"], "n_sent": dr_result["n_sent"],
                                   "n_accepted": n_accepted})),
        )
    conn.commit()
    cur.close()


def build_true_load(feeder, scenario: dict, weather_15min: pd.DataFrame, rng: np.random.Generator,
                    return_truth: bool = False):
    _, truth, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    true_pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)
    pv_by_hh = true_pv.reindex(columns=feeder.household["id"], fill_value=0.0)
    # Not clipped at zero: a household whose PV exceeds its own load exports
    # into the feeder. Clipping it to 0 added phantom load — it produced
    # midday undervoltage that doesn't exist and made overvoltage impossible.
    net_load = true_load - pv_by_hh[true_load.columns].fillna(0.0)
    net_load.columns = feeder.household.set_index("id").loc[net_load.columns, "bus_id"]
    if return_truth:
        return true_load, true_pv, net_load, truth.merge(ownership, on="household_id")
    return true_load, true_pv, net_load


def train_forecast_inputs(feeder, scenario, weather_15min, true_load, true_pv,
                          exclude: list[tuple[pd.Timestamp, pd.Timestamp]] | None = None) -> ForecastInputs:
    """`exclude`: [start, end) spans whose rows are dropped from the load
    model's training set, so a multi-day evaluation (eval/sweep.py) never
    scores a forecast on days the model was fitted to."""
    import holidays as pyholidays

    pv_hh = feeder.household[feeder.household["has_pv"]]
    net_import = true_load.copy()
    for hh_id in pv_hh["id"]:
        if hh_id in true_pv.columns:
            net_import[hh_id] = true_load[hh_id] - true_pv[hh_id].reindex(true_load.index, fill_value=0.0)

    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    unit_pv_all = unit_pv_output_kw(lat, lon, weather_15min)
    pv_kwp_by_hh = dict(zip(pv_hh["id"], pv_hh["pv_kwp"]))
    clear_midday_mask = compute_clear_midday_mask(weather_15min, lat, lon)
    load_est0 = pass0_initial_load_estimate(feeder.household, net_import)
    k = fit_k(load_est0, net_import, unit_pv_all, pv_kwp_by_hh, clear_midday_mask)

    years = sorted(true_load.index.year.unique().tolist())
    holidays_set = {d for d in pyholidays.India(years=years)}
    rng = np.random.default_rng(scenario["sim"]["seed"])
    delay_hours = np.clip(rng.normal(6.0, 2.5, size=len(true_load)), 0.5, 24.0)

    gross_history, static_features = {}, {}
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"],
                                                   np.random.default_rng(scenario["sim"]["seed"]))
    for phase in PHASES:
        hh_ids = feeder.household.loc[feeder.household["phase"] == phase, "id"]
        phase_kw = true_load[hh_ids].sum(axis=1)
        gross_history[phase] = pd.DataFrame({
            "ts_end": phase_kw.index, "gross_kw": phase_kw.to_numpy(),
            "received_at": phase_kw.index + pd.to_timedelta(delay_hours, unit="h"),
        })
        phase_hh = feeder.household[feeder.household["phase"] == phase]
        phase_pv_hh = pv_hh[pv_hh["id"].isin(phase_hh["id"])]
        static_features[phase] = {
            "household_count": len(phase_hh),
            "total_sanctioned_kw": phase_hh["sanctioned_load_kw"].sum(),
            "ac_cooler_share": ownership.set_index("household_id").loc[phase_hh["id"], ["has_ac", "has_cooler"]].any(axis=1).mean(),
            "commercial_share": phase_hh["is_business"].mean(),
            "phase_installed_kwp": phase_pv_hh["pv_kwp"].sum(),
        }

    train_rows = []
    for phase in PHASES:
        history = gross_history[phase]
        target_ts = pd.DatetimeIndex(history["ts_end"])
        feats = build_features(
            target_ts, run_time=target_ts.max(), weather_15min=weather_15min, gross_load_history=history,
            static_features=static_features[phase], holidays_set=holidays_set, festivals_set=set(),
        )
        feats["gross_kw"] = history.set_index("ts_end")["gross_kw"].reindex(feats["ts_end"]).to_numpy()
        feats = feats.dropna(subset=FEATURE_COLUMNS + ["gross_kw"])
        for lo, hi in exclude or []:
            feats = feats[~((feats["ts_end"] >= lo) & (feats["ts_end"] < hi))]
        train_rows.append(feats)
    load_model = train_load_model(pd.concat(train_rows, ignore_index=True))

    return ForecastInputs(load_model=load_model, pv_k=k, weather_15min=weather_15min, lat=lat, lon=lon), \
        gross_history, static_features, holidays_set


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-date", default="2026-04-27", help="the confirmed annual peak-demand day")
    parser.add_argument("--normal-baseline", action=argparse.BooleanOptionalAction, default=True,
                         help="run the Modbus-driven 'normal'/'baseline' day (slow, ~10 min); "
                              "disable to only (re)run the outage day against an existing 'normal' run")
    parser.add_argument("--outage", action=argparse.BooleanOptionalAction, default=True,
                         help="produce the 'outage' recorded run")
    parser.add_argument("--meters", action=argparse.BooleanOptionalAction, default=True,
                         help="write meter_interval rows for 'normal' (needed by settlement)")
    parser.add_argument("--load-shedding", action=argparse.BooleanOptionalAction, default=True,
                         help="produce the 'load_shedding' run (notice -> Pre-outage -> planned cut)")
    parser.add_argument("--surplus", action=argparse.BooleanOptionalAction, default=True,
                         help="produce the 'surplus'/'surplus_baseline' midday-overvoltage runs")
    parser.add_argument("--surplus-date", default="2026-02-11",
                         help="sunniest light-load day (lowest midday feeder net load of the year)")
    args = parser.parse_args()
    plan_date = date_cls.fromisoformat(args.plan_date)

    modbus_host = os.environ.get("MODBUS_HOST", "localhost")
    modbus_port = int(os.environ.get("MODBUS_PORT", "5020"))

    scenario = load_scenario()
    nb = scenario["neighbourhood"]
    rng = np.random.default_rng(scenario["sim"]["seed"])

    print("building feeder and world truth...")
    feeder = build_feeder(scenario)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load, true_pv, net_load = build_true_load(feeder, scenario, weather_15min, rng)

    print("training the forecast models (stands in for cloud/training.py's scheduled job)...")
    inputs, gross_history, static_features, holidays_set = train_forecast_inputs(
        feeder, scenario, weather_15min, true_load, true_pv
    )

    run_time_1400 = pd.Timestamp(plan_date, tz="UTC") - pd.Timedelta(hours=10)
    print(f"\nplanning {plan_date} (14:00 D-1 run)...")
    forecast_artifacts: dict = {}
    plans = run_day_ahead_plan(
        feeder, scenario["battery_blocks"], inputs, plan_date, run_time_1400, run_id="normal",
        v_limit_pct=nb["v_limit_pct"], nominal_v_ln=nb["nominal_v_ln"], transformer_kva=nb["transformer_kva"],
        gross_load_history_by_phase=gross_history, static_features_by_phase=static_features,
        holidays_set=holidays_set, festivals_set=set(), artifacts=forecast_artifacts,
    )
    approved_plans = {
        p: approve_plan(plan, "operator-1", run_time_1400 + pd.Timedelta(hours=4))
        for p, plan in plans.items()
    }

    # DR covers what the battery plan can't: the forecast load still above
    # the transformer's per-phase rating after planned discharge, in the
    # 2-hour evening window where that residual is largest (same rule as
    # eval/sweep.py).
    rating_kw_phase = nb["transformer_kva"] / 3.0 * 0.95
    day_ts = pd.date_range(pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")
    day_local_h = ((day_ts.hour + day_ts.minute / 60.0 + 5.5) % 24).to_numpy()
    excess = {p: np.clip(np.array(plans[p]["forecast_p50_kw"])
                         - np.clip([iv["setpoint_kw"] for iv in plans[p]["intervals"]], 0, None) - rating_kw_phase, 0, None)
              for p in PHASES}
    total_excess = sum(excess.values())
    starts = [i for i in range(96 - 8) if 17.0 <= day_local_h[i] - 0.25 < 21.0]
    w0 = max(starts, key=lambda i: total_excess[i:i + 8].sum())
    worst_phase = max(PHASES, key=lambda p: excess[p][w0:w0 + 8].sum())
    window_start = day_ts[w0] - pd.Timedelta(minutes=15)
    window_end = window_start + pd.Timedelta(hours=2)
    dr_local_start = float(day_local_h[w0] - 0.25)
    target_kw = max(0.5, float(excess[worst_phase][w0:w0 + 8].max()))
    mean_temp_c = float(weather_15min.loc[
        (weather_15min["ts"] >= window_start) & (weather_15min["ts"] < window_end), "temperature_c"
    ].mean())
    print(f"worst phase: {worst_phase}, DR window {window_start} - {window_end}, target {target_kw:.2f}kW")

    conn = db_connect()
    print("seeding default consent (all households, dr_offers granted)...")
    ensure_default_consent(conn)

    # Must exist before anything (including the DR event below) writes a
    # row under run_id="normal" — see init_run()'s docstring. Only when
    # actually regenerating it: init_run() clears dispatch/network_result/
    # sensor_reading/meter_interval too, which would otherwise silently
    # wipe a prior 'normal' run out from under --no-normal-baseline's
    # "just redo the outage day" use case (confirmed: it did exactly
    # this once, before this guard existed).
    if args.normal_baseline:
        init_run(conn, "normal", True, scenario["sim"]["seed"], plan_date, "sim/loop.py recorded run")
        persist_forecast(conn, "normal", forecast_artifacts, nb["dt_id"])

    cur = conn.cursor()
    cur.execute("SELECT household_id, persona FROM household_truth")
    hh_persona = dict(cur.fetchall())
    cur.execute(
        """SELECT h.id, h.is_business, h.sanctioned_load_kw,
                  bool_or(a.kind IN ('ac','cooler')) AS has_ac_or_cooler,
                  bool_or(a.kind = 'pump') AS has_pump
           FROM household h LEFT JOIN appliance a ON a.household_id = h.id
           GROUP BY h.id, h.is_business, h.sanctioned_load_kw"""
    )
    hh_features = {
        row[0]: {"is_business": row[1], "typical_window_kw": 0.35 * row[2],
                  "has_ac_or_cooler": bool(row[3]), "has_pump": bool(row[4])}
        for row in cur.fetchall()
    }
    cur.close()

    print("training the DR bandit offline over simulated history...")
    # Two-phase alpha, not one fixed value. alpha=0.6 (Architecture
    # v3.0 §11.2's illustrative value) left the UCB exploration bonus an
    # order of magnitude larger than the learned prediction even after
    # 6000 training updates (confirmed: bonus ~0.6-0.9 vs predicted_kwh
    # ~0.03-0.13), so a bandit SERVED at alpha=0.6 picks almost-pure
    # exploration forever, which the (1-level) profit penalty then
    # always resolves to level 0.
    #
    # But alpha=0.15 for the *training* phase too (confirmed, the
    # original single-alpha fix) overcorrects into the opposite trap:
    # is_business is the only feature distinguishing households BEFORE
    # any engagement history accumulates, so a low exploration bonus
    # converges almost immediately onto "only businesses are ever worth
    # a paid level" and stops trying level>0 on anyone else — meaning no
    # non-business household's own engagement history (offers_received,
    # past_response_rate, avg_verified_kwh — the signal that's supposed
    # to let the bandit tell a price_sensitive household apart from a
    # non_responsive one with an identical feature vector, Build Spec
    # §5.4) ever gets the chance to diverge. Training wide (0.6) and
    # serving narrow (0.15) is the standard fix: explore enough during
    # the 30 offline days to let real per-household response history
    # form, then exploit that learned signal for the live event.
    bandit = LinUCB(alpha=0.6)
    # Trains against the REAL target households (their real hidden
    # persona, same as world/households.py assigned — legitimate here,
    # this is simulated past history standing in for a bandit that
    # wasn't launched from scratch the morning of its first event, not a
    # live decision), not a throwaway synthetic population. A separate
    # population learns nothing transferable: persona is assigned
    # independently of every feature the bandit can see (confirmed:
    # world/households.py picks it uniformly at random, unrelated to
    # appliance ownership or load), so only a household's OWN
    # accumulated engagement history — offers_received,
    # past_response_rate, avg_verified_kwh — can ever let the bandit
    # tell one household apart from another. Training on a different
    # population builds none of that for the households actually in
    # today's event.
    from eval.sweep import engagement_features, record_response
    engagement_acc = {hh_id: {"offers": 0, "replied": 0, "kwh": 0.0, "ao": 0, "ar": 0, "po": 0, "pr": 0}
                      for hh_id in hh_features}
    for day in range(30):
        # Randomised pilot at real event times/temperatures and a real offer
        # cadence (same as eval/sweep.py pretrain_bandit, which explains why).
        start_h = float(rng.choice(np.arange(17.0, 21.0, 0.25)))
        event = {"start_hour_frac": start_h / 24, "duration_hours": 2.0, "day_of_week_frac": (day % 7) / 7,
                 "forecast_temp_c": float(rng.uniform(28, 38)), "hours_notice": 20.0}
        for hh_id, feat in hh_features.items():
            persona = hh_persona.get(hh_id)
            if persona is None:
                continue
            household = {"typical_window_kw": feat["typical_window_kw"], "window_variability": 0.3,
                         "has_ac_or_cooler": float(feat["has_ac_or_cooler"]), "has_pump": float(feat["has_pump"]),
                         "is_business": float(feat["is_business"])}
            acc = engagement_acc[hh_id]
            gap = 30.0 if acc["offers"] == 0 else float(rng.integers(3, 31))
            engagement = engagement_features(acc, gap, int(rng.integers(0, 4)))
            level = float(rng.choice(LEVELS))
            choice = {"level": level, "x": build_feature_vector(household, engagement, event, level)}
            response = respond_to_offer(
                persona, level, window_baseline_kw=feat["typical_window_kw"],
                window_hours=event["duration_hours"], window_start_local_hour=start_h,
                mean_temperature_c=event["forecast_temp_c"], days_since_last_offer=gap, rng=rng,
            )
            bandit.update(choice["x"], response.verified_kwh)
            record_response(acc, level, response)
    print("bandit trained.")
    # Narrow back down for the live decision: theta is already learned,
    # so the UCB bonus only needs to be large enough to keep adapting,
    # not large enough to keep randomly trying arms on the one day that
    # actually gets served to real households and billed.
    bandit.alpha = 0.15

    # The real event starts "rested" (days_since_last_offer reset) since
    # the 30 synthetic training days represent history before this
    # programme's real, regulatorily-capped offer tracking began — only
    # offers_received/past_response_rate/avg_verified_kwh, the household-
    # differentiating signal the bandit actually learned from, carry
    # forward. offers_this_month stays real (0, this being day one).
    engagement_override = {hh_id: engagement_features(acc, 10.0, 0) for hh_id, acc in engagement_acc.items()}

    print("running the real DR event (selection + SMS send)...")
    sms_url = os.environ.get("SMS_URL", "http://localhost:8012")
    dr_result = run_dr_event(
        conn, bandit, run_id="normal", event_id=f"EVT-{plan_date.isoformat()}-{worst_phase}-{int(dr_local_start):02d}{int(round(dr_local_start % 1 * 60)):02d}",
        phase=worst_phase, window_start=window_start.to_pydatetime(), window_end=window_end.to_pydatetime(),
        target_kw=target_kw, v_rupees_per_kwh=V_RUPEES_PER_KWH, event_date=plan_date,
        forecast_temp_c=mean_temp_c, hours_notice=20.0, sms_url=sms_url, rng=rng,
        engagement_override=engagement_override,
    )
    print(f"DR event: {dr_result['n_sent']} offers sent of {dr_result['n_eligible']} eligible "
          f"({dr_result['n_holdout']} held out), predicted {dr_result['predicted_kw']:.2f}kW")

    # Run personas' actual responses now (rather than only the bandit's
    # offline training) so the "normal" run's household loads really
    # reflect who accepted — this is what run_one_day subtracts from
    # true load in the DR window.

    window_hours = (window_end - window_start).total_seconds() / 3600.0
    window_idx = pd.date_range(window_start, window_end, freq="15min", inclusive="left")
    bus_by_hhid = dict(zip(feeder.household["id"], feeder.household["bus_id"]))

    accepted_reduction_kw: dict[str, float] = {}
    for offer in dr_result["offers"]:
        if not offer["sent"]:
            continue
        persona = hh_persona.get(offer["household_id"])
        if persona is None:
            continue

        bus_id = bus_by_hhid.get(offer["household_id"])
        baseline_series = net_load[bus_id].reindex(window_idx) if bus_id in net_load.columns else None
        window_baseline_kw = float(baseline_series.mean()) if baseline_series is not None and baseline_series.notna().any() else 1.0

        response = respond_to_offer(
            persona, offer["level"], window_baseline_kw=window_baseline_kw,
            window_hours=window_hours, window_start_local_hour=dr_local_start,
            mean_temperature_c=mean_temp_c, days_since_last_offer=30, rng=rng,
        )
        if response.accepted:
            accepted_reduction_kw[offer["household_id"]] = response.verified_kwh / window_hours

    cur = conn.cursor()
    for hh_id, reduction_kw in accepted_reduction_kw.items():
        cur.execute(
            "UPDATE dr_offer SET replied = TRUE, verified_kwh = %s WHERE run_id='normal' AND event_id=%s AND household_id=%s",
            (reduction_kw * window_hours, dr_result["event_id"], hh_id),
        )
    conn.commit()
    cur.close()
    print(f"{len(accepted_reduction_kw)} households accepted; total reduction "
          f"{sum(accepted_reduction_kw.values()):.2f}kW")

    net = build_pandapower_net(feeder, nb["transformer_kva"], nb["nominal_v_ln"])

    if args.normal_baseline:
        print("\n=== LEO-on run ('normal') ===")
        t0 = time.perf_counter()
        run_one_day(
            conn, "normal", True, plan_date, scenario, feeder, net, net_load, approved_plans, accepted_reduction_kw,
            nb["v_limit_pct"], nb["nominal_v_ln"], modbus_host, modbus_port, rng,
            dr_window=(window_start, window_end),
        )
        write_dr_window_events(conn, "normal", dr_result, window_start, window_end, len(accepted_reduction_kw))
        print(f"took {time.perf_counter()-t0:.1f}s")

        print("\n=== baseline run (battery + DR disabled) ===")
        init_run(conn, "baseline", False, scenario["sim"]["seed"], plan_date,
                 "sim/loop.py recorded run (baseline: battery disabled)")
        t0 = time.perf_counter()
        run_one_day(
            conn, "baseline", False, plan_date, scenario, feeder, net, net_load, None, {},
            nb["v_limit_pct"], nb["nominal_v_ln"], modbus_host, modbus_port, rng,
        )
        print(f"took {time.perf_counter()-t0:.1f}s")

    if args.meters:
        print("\n=== writing meter_interval rows for 'normal' ===")
        target_ts = pd.date_range(pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")
        interval_hours = 0.25
        write_meter_intervals(
            conn, "normal", feeder, true_load, true_pv, target_ts, accepted_reduction_kw,
            (window_start, window_end), interval_hours, rng,
        )
        print("done.")

    # Storyboard beat 6: an outage at 19:40 IST, restored 90 minutes later.
    # IST -> UTC: subtract 5:30.
    outage_start = pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(hours=19 - 5, minutes=40 - 30)
    outage_end = outage_start + pd.Timedelta(minutes=90)

    if args.outage:
        print("\n=== unplanned outage run ('outage') ===")
        run_outage_day(conn, "outage", plan_date, scenario, feeder, outage_start, outage_end, rng,
                       source_run_id="normal", cause="upstream_fault")

    if args.load_shedding:
        # §14 "Load shedding": the DISCOM publishes a schedule; LEO enters
        # Pre-outage on the notice (§9.3), raises the reserve on all three
        # blocks and alerts households, then the cut happens on schedule.
        from gateway.orchestrator.modes import ModeState, transition
        print("\n=== planned load-shedding run ('load_shedding') ===")
        notice_ts = pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(hours=14 - 5, minutes=0 - 30)
        init_run(conn, "load_shedding", True, scenario["sim"]["seed"], plan_date,
                 f"sim/loop.py recorded run: DISCOM load-shedding notice {notice_ts}, cut {outage_start}–{outage_end}")
        copy_dr_rows(conn, "normal", "load_shedding")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'load_shedding_notice','upstream','discom',%s)",
            ("load_shedding", notice_ts.to_pydatetime(), psycopg2.extras.Json({
                "scheduled_start": outage_start.isoformat(), "scheduled_end": outage_end.isoformat(),
                "reason": "DISCOM short of power on the evening ramp"})),
        )
        _, tr = transition(ModeState(mode="normal"), notice_ts.to_pydatetime(), pre_outage_risk=True)
        cur.execute(
            "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
            ("load_shedding", tr.ts, tr.from_mode, tr.to_mode, tr.owner, "DISCOM load-shedding notice"),
        )
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'alert_sent','all','leo',%s)",
            ("load_shedding", (notice_ts + pd.Timedelta(minutes=5)).to_pydatetime(), psycopg2.extras.Json({
                "n_households": int(len(feeder.household)), "channel": "sms",
                "message": "Scheduled power cut 19:40-21:10 tonight. Please charge phones and home inverters now."})),
        )
        conn.commit()
        cur.close()
        run_one_day(
            conn, "load_shedding", True, plan_date, scenario, feeder, net, net_load, approved_plans,
            accepted_reduction_kw, nb["v_limit_pct"], nb["nominal_v_ln"], modbus_host, modbus_port, rng,
            dr_window=(window_start, window_end), pre_outage=(notice_ts, outage_start),
        )
        write_dr_window_events(conn, "load_shedding", dr_result, window_start, window_end, len(accepted_reduction_kw))
        run_outage_day(conn, "load_shedding", plan_date, scenario, feeder, outage_start, outage_end, rng,
                       source_run_id="load_shedding", cause="load_shedding")

    if args.surplus:
        # §14 "Midday overvoltage": a sunny, light-load day where rooftop PV
        # exports into the feeder. No DR here — LEO's DR engine only does
        # evening reduction offers; the architecture's load-shift-into-midday
        # offers aren't built.
        surplus_date = date_cls.fromisoformat(args.surplus_date)
        surplus_run_time = pd.Timestamp(surplus_date, tz="UTC") - pd.Timedelta(hours=10)
        print(f"\n=== midday-surplus day {surplus_date} ('surplus' / 'surplus_baseline') ===")
        surplus_artifacts: dict = {}
        surplus_plans = run_day_ahead_plan(
            feeder, scenario["battery_blocks"], inputs, surplus_date, surplus_run_time, run_id="surplus",
            v_limit_pct=nb["v_limit_pct"], nominal_v_ln=nb["nominal_v_ln"], transformer_kva=nb["transformer_kva"],
            gross_load_history_by_phase=gross_history, static_features_by_phase=static_features,
            holidays_set=holidays_set, festivals_set=set(), artifacts=surplus_artifacts,
        )
        surplus_approved = {
            p: approve_plan(plan, "operator-1", surplus_run_time + pd.Timedelta(hours=4))
            for p, plan in surplus_plans.items()
        }
        init_run(conn, "surplus", True, scenario["sim"]["seed"], surplus_date,
                 "sim/loop.py recorded run: sunny low-demand day, midday PV export")
        persist_forecast(conn, "surplus", surplus_artifacts, nb["dt_id"])
        run_one_day(conn, "surplus", True, surplus_date, scenario, feeder, net, net_load, surplus_approved, {},
                    nb["v_limit_pct"], nb["nominal_v_ln"], modbus_host, modbus_port, rng)
        init_run(conn, "surplus_baseline", False, scenario["sim"]["seed"], surplus_date,
                 "sim/loop.py recorded run: sunny low-demand day, battery disabled")
        run_one_day(conn, "surplus_baseline", False, surplus_date, scenario, feeder, net, net_load, None, {},
                    nb["v_limit_pct"], nb["nominal_v_ln"], modbus_host, modbus_port, rng)

    conn.close()


if __name__ == "__main__":
    main()
