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
    compute_phase_limits, get_bus_index,
)
from gateway.forecast.load_model import build_features, FEATURE_COLUMNS, train as train_load_model
from gateway.forecast.pv_model import unit_pv_output_kw, fit_k, compute_clear_midday_mask, pass0_initial_load_estimate
from gateway.forecast.run import ForecastInputs, run_day_ahead_plan, approve_plan
from gateway.orchestrator.live_rules import apply_live_rule
from cloud.dr_engine.linucb import LinUCB, choose_level
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
V_RUPEES_PER_KWH = 6.0
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
        "dispatch", "network_result", "phase_limit", "plan", "mode_transition",
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
            await asyncio.sleep(1.3)  # let the mock's physics loop (1s period) tick at least once
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
) -> None:
    sim_start = pd.Timestamp(plan_date, tz="UTC")
    target_ts = pd.date_range(sim_start + pd.Timedelta(minutes=15), periods=96, freq="15min")

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

        if leo_enabled and accepted_reduction_kw:
            for hh_id, reduction_kw in accepted_reduction_kw.items():
                bus_id = bus_id_by_hhid.get(hh_id)
                if bus_id in loads:
                    loads[bus_id] = max(0.0, loads[bus_id] - reduction_kw)

        update_household_loads(net, feeder, loads)

        sgen_indices = []
        dispatch_rows = []
        if leo_enabled and approved_plans is not None:
            for phase in PHASES:
                if state.trip_until[phase] is not None:
                    continue
                plan_iv = approved_plans[phase]["intervals"][i]
                planned_kw = plan_iv["setpoint_kw"]

                run_power_flow(net)
                base_viol = detect_violations(net, v_limit_pct)
                far_end_bus = far_end_sensor_bus.get(phase)
                far_end_row = base_viol[base_viol["bus_id"] == far_end_bus]
                far_end_v = float(far_end_row["voltage_v"].iloc[0]) if not far_end_row.empty else nominal_v_ln

                limits = compute_phase_limits(
                    net, feeder.root, phase, v_limit_pct,
                    next(b["power_kw"] for b in scenario["battery_blocks"] if b["phase"] == phase),
                )
                corrected = apply_live_rule(
                    planned_kw, far_end_v, nominal_v_ln, v_limit_pct,
                    limits.max_charge_kw, limits.max_discharge_kw,
                )

                telemetry = _dispatch_battery_sync(
                    UNIT_ID_BY_PHASE[phase], corrected.setpoint_kw, modbus_host, modbus_port
                )
                actual_kw = telemetry["actual_kw"]

                if actual_kw != 0:
                    letter = PHASE_LETTER[phase]
                    idx = pp.create_asymmetric_sgen(net, bus=root_idx, **{f"p_{letter}_mw": actual_kw / 1000.0})
                    sgen_indices.append(idx)

                dispatch_rows.append((phase, planned_kw, actual_kw, telemetry["soc"], corrected.rule_triggered))

        run_power_flow(net)
        violations = detect_violations(net, v_limit_pct)

        for phase in PHASES:
            phase_v = violations[violations["phase"] == phase]
            max_loading = phase_v["loading_pct"].max() if not phase_v.empty and phase_v["loading_pct"].notna().any() else 0.0
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

        for phase, planned_kw, actual_kw, soc, rule in dispatch_rows:
            block_id = f"BATT-{phase}"
            cur.execute(
                """INSERT INTO dispatch (run_id, block_id, ts_end, setpoint_kw, actual_kw,
                       soc_after, mode, rule_triggered)
                   VALUES (%s,%s,%s,%s,%s,%s,'normal',%s)""",
                (run_id, block_id, ts.to_pydatetime(), planned_kw, actual_kw, soc, rule),
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
) -> dict:
    """The 'outage' recorded run (Build Spec v1.0 §7.4, §7.6): the SAME
    day, same plan and DR as 'normal' — copied wholesale for every
    interval outside the outage window, since nothing about those
    intervals differs — plus a real unplanned upstream outage, backup
    allocation via gateway/outage.py (Owner A's pure functions, same
    ones eval/run_arms.py's M3 computation already uses), and staged
    restoration.

    During the outage window there is no grid source for runpp_3ph to
    solve against, so no network_result rows are written for it — the
    backup circuit is a separate, physically disconnected circuit
    (anti-islanding, §C2), not a scaled-down version of the main feeder.
    What the operator/DISCOM actually see during an outage is exactly
    what's written here: event rows, premise_meter rows, mode transitions.
    """
    from gateway.outage import register_backup_premises, allocate_backup_power, stage_restoration
    from gateway.orchestrator.modes import ModeState, transition

    sim_start = pd.Timestamp(plan_date, tz="UTC")
    target_ts = pd.date_range(sim_start + pd.Timedelta(minutes=15), periods=96, freq="15min")

    init_run(conn, run_id, True, scenario["sim"]["seed"], plan_date,
             f"sim/loop.py recorded run: unplanned upstream outage {outage_start}–{outage_end}")
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO plan (run_id, plan_date, phase, ts_end, setpoint_kw, mode, planner,
               reserve_kwh, approved_at, approved_by)
           SELECT %s, plan_date, phase, ts_end, setpoint_kw, mode, planner, reserve_kwh, approved_at, approved_by
           FROM plan WHERE run_id = 'normal'""",
        (run_id,),
    )
    cur.execute(
        """INSERT INTO dispatch (run_id, block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered)
           SELECT %s, block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered
           FROM dispatch WHERE run_id = 'normal' AND (ts_end < %s OR ts_end >= %s)""",
        (run_id, outage_start.to_pydatetime(), outage_end.to_pydatetime()),
    )
    cur.execute(
        """INSERT INTO network_result (run_id, ts_end, bus_id, phase, voltage_v, loading_pct, violation, is_forecast)
           SELECT %s, ts_end, bus_id, phase, voltage_v, loading_pct, violation, is_forecast
           FROM network_result WHERE run_id = 'normal' AND (ts_end < %s OR ts_end >= %s)
           ON CONFLICT (run_id, ts_end, bus_id, phase, is_forecast) DO NOTHING""",
        (run_id, outage_start.to_pydatetime(), outage_end.to_pydatetime()),
    )
    conn.commit()
    print(f"{run_id}: copied plan/dispatch/network_result for the non-outage hours from 'normal'")

    # Outage start: upstream, all three phases dark.
    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'grid_loss','upstream','sim',%s)",
        (run_id, outage_start.to_pydatetime(), psycopg2.extras.Json({"cause": "upstream"})),
    )
    for phase in PHASES:
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'power_fail',%s,'sensor',%s)",
            (run_id, outage_start.to_pydatetime(), f"phase:{phase}", psycopg2.extras.Json({})),
        )

    mode_state = ModeState(mode="normal")
    mode_state, tr = transition(mode_state, outage_start.to_pydatetime(), grid_lost=True)
    cur.execute(
        "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, tr.ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason),
    )

    premise_backup = register_backup_premises(feeder.household)
    household_phase_by_id = dict(zip(feeder.household["id"], feeder.household["phase"]))
    nominal_v_ln = scenario["neighbourhood"]["nominal_v_ln"]
    # Reserve floor (15% SoC, same floor run_one_day/plan_rule_based holds)
    # converted to an available-power figure for the outage's duration —
    # the backup port, not the grid port, is what's drawing it now.
    outage_hours = (outage_end - outage_start).total_seconds() / 3600.0
    available_kw_by_phase = {
        b["phase"]: (0.15 * b["capacity_kwh"]) / max(outage_hours, 0.25)
        for b in scenario["battery_blocks"]
    }
    allocations = allocate_backup_power(premise_backup, household_phase_by_id, available_kw_by_phase, nominal_v_ln)

    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'backup_start','upstream','sim',%s)",
        (run_id, outage_start.to_pydatetime(),
         psycopg2.extras.Json({"n_premises": len(allocations), "total_kw": sum(a.allocated_kw for a in allocations)})),
    )

    outage_intervals = [ts for ts in target_ts if outage_start <= ts < outage_end]
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

    # Restoration.
    cur.execute(
        "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'grid_return','upstream','sim',%s)",
        (run_id, outage_end.to_pydatetime(), psycopg2.extras.Json({})),
    )
    mode_state, tr = transition(mode_state, outage_end.to_pydatetime(), grid_returned=True)
    cur.execute(
        "INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, tr.ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason),
    )

    batches = stage_restoration(premise_backup)
    batch_gap = timedelta(minutes=3)
    for i, batch in enumerate(batches):
        batch_ts = outage_end.to_pydatetime() + i * batch_gap
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'restore',%s,'sim',%s)",
            (run_id, batch_ts, "upstream", psycopg2.extras.Json({"batch": i, "household_ids": batch})),
        )
    restoration_complete_ts = outage_end.to_pydatetime() + len(batches) * batch_gap
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
    print(f"{run_id}: outage {outage_start}-{outage_end}, {len(allocations)} premises allocated backup, "
          f"{len(batches)} restoration batches")
    return {"n_premises": len(allocations), "allocations": allocations, "batches": batches}


def build_true_load(feeder, scenario: dict, weather_15min: pd.DataFrame, rng: np.random.Generator):
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    true_pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)
    pv_by_hh = true_pv.reindex(columns=feeder.household["id"], fill_value=0.0)
    net_load = (true_load - pv_by_hh[true_load.columns].fillna(0.0)).clip(lower=0.0)
    net_load.columns = feeder.household.set_index("id").loc[net_load.columns, "bus_id"]
    return true_load, true_pv, net_load


def train_forecast_inputs(feeder, scenario, weather_15min, true_load, true_pv) -> ForecastInputs:
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
        train_rows.append(feats.dropna(subset=FEATURE_COLUMNS + ["gross_kw"]))
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
    plans = run_day_ahead_plan(
        feeder, scenario["battery_blocks"], inputs, plan_date, run_time_1400, run_id="normal",
        v_limit_pct=nb["v_limit_pct"], nominal_v_ln=nb["nominal_v_ln"], transformer_kva=nb["transformer_kva"],
        gross_load_history_by_phase=gross_history, static_features_by_phase=static_features,
        holidays_set=holidays_set, festivals_set=set(),
    )
    approved_plans = {
        p: approve_plan(plan, "operator-1", run_time_1400 + pd.Timedelta(hours=4))
        for p, plan in plans.items()
    }

    worst_phase = max(PHASES, key=lambda p: sum(iv["setpoint_kw"] for iv in plans[p]["intervals"] if iv["setpoint_kw"] > 0))
    # IST -> UTC: subtract 5:30. DR_WINDOW_IST is (19, 21) -> 13:30-15:30 UTC.
    window_start = pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(hours=DR_WINDOW_IST[0] - 5, minutes=-30)
    window_end = window_start + pd.Timedelta(hours=DR_WINDOW_IST[1] - DR_WINDOW_IST[0])
    target_kw = max(iv["setpoint_kw"] for iv in plans[worst_phase]["intervals"])
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
    # alpha=0.6 (Architecture v3.0 §11.2's illustrative value) left the
    # UCB exploration bonus an order of magnitude larger than the
    # learned prediction even after 6000 training updates (confirmed:
    # bonus ~0.6-0.9 vs predicted_kwh ~0.03-0.13), so score() was still
    # picking almost-pure exploration, which the (1-level) profit
    # penalty then always resolves to level 0. 0.15 is where the learned
    # signal actually starts to compete.
    bandit = LinUCB(alpha=0.15)
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
    engagement_acc = {hh_id: {"offers": 0, "replied": 0, "verified_kwh_sum": 0.0} for hh_id in hh_features}
    for day in range(30):
        event = {"start_hour_frac": 19 / 24, "duration_hours": 2.0, "day_of_week_frac": (day % 7) / 7,
                 "forecast_temp_c": float(rng.uniform(24, 36)), "hours_notice": 20.0}
        for hh_id, feat in hh_features.items():
            persona = hh_persona.get(hh_id)
            if persona is None:
                continue
            household = {"typical_window_kw": feat["typical_window_kw"], "window_variability": 0.3,
                         "has_ac_or_cooler": float(feat["has_ac_or_cooler"]), "has_pump": float(feat["has_pump"]),
                         "is_business": float(feat["is_business"])}
            acc = engagement_acc[hh_id]
            engagement = {
                "offers_received": acc["offers"],
                "past_response_rate": acc["replied"] / acc["offers"] if acc["offers"] else 0.0,
                "avg_verified_kwh": acc["verified_kwh_sum"] / acc["offers"] if acc["offers"] else 0.0,
                "days_since_last_offer": 30.0 if acc["offers"] == 0 else 1.0,
            }
            choice = choose_level(bandit, household, engagement, event, V_RUPEES_PER_KWH)
            response = respond_to_offer(
                persona, choice["level"], window_baseline_kw=feat["typical_window_kw"],
                window_hours=event["duration_hours"], window_start_local_hour=19.0,
                mean_temperature_c=event["forecast_temp_c"], days_since_last_offer=engagement["days_since_last_offer"],
                rng=rng,
            )
            bandit.update(choice["x"], response.verified_kwh)
            acc["offers"] += 1
            acc["replied"] += int(response.accepted)
            acc["verified_kwh_sum"] += response.verified_kwh
    print("bandit trained.")

    # The real event starts "rested" (days_since_last_offer reset) since
    # the 30 synthetic training days represent history before this
    # programme's real, regulatorily-capped offer tracking began — only
    # offers_received/past_response_rate/avg_verified_kwh, the household-
    # differentiating signal the bandit actually learned from, carry
    # forward. offers_this_month stays real (0, this being day one).
    engagement_override = {
        hh_id: {
            "offers_received": acc["offers"],
            "past_response_rate": acc["replied"] / acc["offers"] if acc["offers"] else 0.0,
            "avg_verified_kwh": acc["verified_kwh_sum"] / acc["offers"] if acc["offers"] else 0.0,
            "days_since_last_offer": 10.0,
            "offers_this_month": 0,
        }
        for hh_id, acc in engagement_acc.items()
    }

    print("running the real DR event (selection + SMS send)...")
    sms_url = os.environ.get("SMS_URL", "http://localhost:8012")
    dr_result = run_dr_event(
        conn, bandit, run_id="normal", event_id=f"EVT-{plan_date.isoformat()}-{worst_phase}-{DR_WINDOW_IST[0]}00",
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
            window_hours=window_hours, window_start_local_hour=DR_WINDOW_IST[0],
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
        )
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

    if args.outage:
        print("\n=== outage run ('outage') ===")
        # Storyboard beat 6: an unplanned outage at 19:40 IST, restored ~90
        # minutes later. IST -> UTC: subtract 5:30.
        outage_start = pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(hours=19 - 5, minutes=40 - 30)
        outage_end = outage_start + pd.Timedelta(minutes=90)
        run_outage_day(conn, "outage", plan_date, scenario, feeder, outage_start, outage_end, rng)

    conn.close()


if __name__ == "__main__":
    main()
