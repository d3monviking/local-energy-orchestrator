"""sim/loop.py — the simulated world, run in lockstep with the edge gateway.

Records the demo scenarios into Postgres under a `run_id` each. The world
here owns the truth (household loads, rooftop PV, outages, the three-phase
power flow, fuse trips); every LEO decision is made by the running edge
gateway (gateway/service.py), which sees the world only through its own
devices: sensor uplinks on MQTT, inverters and premise meters on Modbus,
a DISCOM notice relayed over REST. sim/field.py is that boundary.

The cloud-side steps of a recorded day run here too, as they would in the
operator cloud: the day-ahead forecast and plan (gateway/forecast/run.py),
operator approval, the DR engine's offers (cloud/dr_engine) and the
households' responses (sim/personas.py), and the day-late meter files.

Needs the stack up (`docker compose up -d`): Postgres, Mosquitto, clock,
mock-modbus, mock-modbus-meters, mock-sms and gateway.

Usage:
    python -m sim.loop                    # all scenarios
    python -m sim.loop --no-surplus       # skip the sunny-surplus pair
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import date as date_cls

import numpy as np
import pandas as pd
import pandapower as pp
import psycopg2
import psycopg2.extras

from world.feeder import build_feeder, load_scenario
from world.households import assign_ownership_and_truth, generate_true_load
from world.pv_truth import assign_pv_truth_params, generate_true_pv
from world.weather import fetch_scenario_weather, to_15min
from gateway.network_model import (
    build_pandapower_net, update_household_loads, run_power_flow, detect_violations, get_bus_index,
)
from gateway.forecast.load_model import build_features, FEATURE_COLUMNS, train as train_load_model
from gateway.forecast.pv_model import unit_pv_output_kw, fit_k, compute_clear_midday_mask, pass0_initial_load_estimate
from gateway.forecast.run import ForecastInputs, run_day_ahead_plan, approve_plan
from cloud.dr_engine.linucb import LinUCB, build_feature_vector, LEVELS
from cloud.dr_engine.selection import run_dr_event, ensure_default_consent
from sim.personas import respond_to_offer
from sim import measure
from sim.field import Field, ct_reading

PHASES = ["R", "Y", "B"]
PHASE_LETTER = {"R": "a", "Y": "b", "B": "c"}
INTERVAL = pd.Timedelta(minutes=15)

IST_OFFSET_HOURS = 5.5
TRIP_THRESHOLD_PCT = 100.0
TRIP_CONSECUTIVE_INTERVALS = 4
TRIP_REPAIR_INTERVALS = 8

V_RUPEES_PER_KWH = 7.92  # IEX evening peak premium (research note); energy value of a kWh cut


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


@dataclass
class ArmState:
    trip_until: dict = field(default_factory=lambda: {"R": None, "Y": None, "B": None})
    consecutive_overload: dict = field(default_factory=lambda: {"R": 0, "Y": 0, "B": 0})


@dataclass(frozen=True)
class Outage:
    """Loss of grid supply at the transformer, as the world experiences it.
    LEO is not told about it: its sensors go dark and its inverters lose
    the grid at their terminals."""
    start: pd.Timestamp
    end: pd.Timestamp
    scope: str = "upstream"  # 'upstream' | 'phase:R' | ...

    @property
    def phases(self) -> list[str]:
        return list(PHASES) if self.scope == "upstream" else [self.scope.split(":", 1)[1]]

    def down_at(self, ts) -> bool:
        return self.start <= ts < self.end

    def covers_interval(self, ts_end) -> bool:
        """An interval touched by the outage has no grid-connected power flow."""
        return self.start < ts_end <= self.end


@dataclass(frozen=True)
class Notice:
    """A DISCOM load-shedding schedule, published `notice_ts`."""
    notice_ts: pd.Timestamp
    start: pd.Timestamp
    end: pd.Timestamp
    reason: str = "DISCOM short of power on the evening ramp"


def init_run(conn, run_id: str, leo_enabled: bool, seed: int, plan_date: date_cls, notes: str) -> None:
    """Creates (or re-creates) the `run` row every other table's run_id
    foreign key depends on. Must happen before ANYTHING else writes a
    row under this run_id — including cloud/dr_engine.run_dr_event(),
    which is called before run_day() in main() below.
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


def run_day(
    conn,
    run_id: str,
    leo_enabled: bool,
    plan_date: date_cls,
    scenario: dict,
    feeder,
    net,
    household_load_full_kw: pd.DataFrame,
    gross_load_kw: pd.DataFrame,
    approved_plans: dict | None,
    accepted_reduction_kw: dict[str, float],
    devices: Field,
    rng: np.random.Generator,
    dr_window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    load_adjust: dict[str, np.ndarray] | None = None,
    outages: tuple[Outage, ...] = (),
    notice: Notice | None = None,
    forecast_run_id: str = "normal",
) -> dict:
    """One simulated day, with every LEO decision made by the edge gateway.

    The world owns the truth: household loads (net of rooftop PV, minus
    whatever DR households actually delivered), outages, the three-phase
    power flow, fuse trips. Per 15-minute interval:

      1. the sensors transmit (sim/field.py): last interval's voltages,
         the busbar CTs' phase load now, power-fail / restore events;
      2. the clock moves and the gateway acts (reads its devices, writes
         setpoints and backup limits) — an outage or a DISCOM notice
         mid-interval gets its own tick at the exact moment;
      3. the inverters advance (BMS, anti-islanding, backup port);
      4. the power flow runs with what the batteries actually did;
      5. sustained conductor overload trips the phase's fuse.

    `household_load_full_kw` is keyed by bus id (net load), `gross_load_kw`
    by household id (what a backup premise wants to draw). With
    `leo_enabled` False the gateway runs in shadow mode and the batteries
    are not used: the no-LEO baseline.
    """
    nb = scenario["neighbourhood"]
    v_limit_pct, nominal_v_ln = nb["v_limit_pct"], nb["nominal_v_ln"]
    day_start = pd.Timestamp(plan_date, tz="UTC")
    target_ts = pd.date_range(day_start + INTERVAL, periods=96, freq="15min")

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

    devices.pause_clock()
    devices.install({b["phase"]: (b["capacity_kwh"], b["power_kw"]) for b in scenario["battery_blocks"]})
    devices.gateway_start(run_id, plan_date, control=leo_enabled, plan_run_id=run_id, forecast_run_id=forecast_run_id)

    bus_id_by_hhid = dict(zip(feeder.household["id"], feeder.household["bus_id"]))
    hh_bus_by_phase = {p: feeder.household.loc[feeder.household["phase"] == p, "bus_id"].tolist() for p in PHASES}
    sensors = list(feeder.sensor.itertuples())
    premises_by_phase = {p: devices.premises.loc[devices.premises["phase"] == p, "household_id"].tolist() for p in PHASES}
    root_idx = get_bus_index(net, feeder.root)
    state = ArmState()
    meter_kwh = {h: 0.0 for h in devices.premises["household_id"]}

    def voltages_now(violations: pd.DataFrame) -> dict:
        v = violations.set_index(["bus_id", "phase"])["voltage_v"]
        return {s.id: float(v.get((str(s.bus_id), s.phase), nominal_v_ln)) for s in sensors}

    # What the sensors saw just before the day starts: the first interval's
    # load with the batteries idle.
    first = household_load_full_kw.loc[target_ts[0]].to_dict() if target_ts[0] in household_load_full_kw.index else {}
    update_household_loads(net, feeder, first)
    run_power_flow(net)
    prev_v = voltages_now(detect_violations(net, v_limit_pct))

    n_trips = 0
    for i, ts in enumerate(target_ts):
        start = ts - INTERVAL
        loads = household_load_full_kw.loc[ts].to_dict() if ts in household_load_full_kw.index else {}

        for phase in PHASES:
            if state.trip_until[phase] is not None and ts < state.trip_until[phase]:
                for bus_id in hh_bus_by_phase[phase]:
                    loads[bus_id] = 0.0
            elif state.trip_until[phase] is not None and ts >= state.trip_until[phase]:
                state.trip_until[phase] = None
                state.consecutive_overload[phase] = 0

        # DR only inside its window; automated DR as per-bus adjustments.
        in_dr_window = dr_window is not None and dr_window[0] <= ts < dr_window[1]
        if leo_enabled and accepted_reduction_kw and in_dr_window:
            for hh_id, reduction_kw in accepted_reduction_kw.items():
                bus_id = bus_id_by_hhid.get(hh_id)
                if bus_id in loads:
                    loads[bus_id] = loads[bus_id] - reduction_kw
        if leo_enabled and load_adjust:
            for bus_id, adj in load_adjust.items():
                if bus_id in loads:
                    loads[bus_id] = loads[bus_id] - float(adj[i])

        def phase_up(p: str, at) -> bool:
            return not any(o.down_at(at) and p in o.phases for o in outages)

        # Moments inside this interval where something happens to LEO.
        cuts = {start}
        for o in outages:
            cuts |= {x for x in (o.start, o.end) if start < x < ts}
        if notice is not None and start < notice.notice_ts < ts:
            cuts.add(notice.notice_ts)
        cuts = sorted(cuts)

        energy_kwh = {p: 0.0 for p in PHASES}
        for k, a in enumerate(cuts):
            b = cuts[k + 1] if k + 1 < len(cuts) else ts
            if leo_enabled and notice is not None and a == notice.notice_ts:
                devices.relay_notice(notice.notice_ts, notice.start, notice.end, notice.reason)
            for o in outages:
                if a in (o.start, o.end):
                    back = a == o.end
                    for p in o.phases:
                        devices.set_grid(p, back)
                    for s in sensors:
                        if s.phase in o.phases:
                            devices.power_event(s, a, "power_restore" if back else "power_fail",
                                                None if back else prev_v.get(s.id))
            if a == start:
                for s in sensors:
                    tripped_far = s.placement == "far_end" and state.trip_until[s.phase] is not None
                    supply = phase_up(s.phase, a) and not tripped_far
                    reading = measure.sensor_reading(prev_v.get(s.id) or nominal_v_ln, supply, rng)
                    if reading is None:
                        continue  # a LoRa packet that never arrives
                    ct = None
                    if s.placement == "busbar" and supply:
                        ct = ct_reading(sum(loads.get(bus, 0.0) for bus in hh_bus_by_phase[s.phase]), rng)
                    devices.uplink(s, a, reading["voltage_v"] if prev_v.get(s.id) is not None else None, supply, ct)
            devices.tick(a)

            if not leo_enabled:
                continue
            # Backup circuit: each premise draws what it wants up to the
            # limit the gateway set on its meter; the block supplies what it can.
            want = {}
            for p in PHASES:
                total = 0.0
                if not phase_up(p, a):
                    for h in premises_by_phase[p]:
                        gross = max(0.0, float(gross_load_kw.at[ts, h])) if h in gross_load_kw.columns else 0.0
                        lim = devices.premise_limit_kw(h)
                        want[h] = (p, min(gross, lim), gross > lim + 1e-9)
                        total += want[h][1]
                devices.set_backup_demand(p, total)
            seg_h = (b - a).total_seconds() / 3600.0
            tel = devices.advance(PHASES, (b - a).total_seconds())
            for p in PHASES:
                energy_kwh[p] += tel[p]["power_kw"] * seg_h
            demand_by_phase = {p: sum(w[1] for w in want.values() if w[0] == p) for p in PHASES}
            for h, (p, kw, limited) in want.items():
                frac = tel[p]["backup_kw"] / demand_by_phase[p] if demand_by_phase[p] > 0 else 0.0
                draw = kw * frac
                meter_kwh[h] += draw * seg_h
                devices.meter_record(h, meter_kwh[h], draw * 1000.0 / nominal_v_ln, limited)
            for o in outages:  # premises back on the grid: their backup meters go quiet
                if a == o.end:
                    for p in o.phases:
                        for h in premises_by_phase[p]:
                            devices.meter_record(h, meter_kwh[h], 0.0, False)

        # ---- the power flow, with what the batteries actually did.
        down = [p for p in PHASES if any(o.covers_interval(ts) and p in o.phases for o in outages)]
        if len(down) == len(PHASES):
            prev_v = {s.id: None for s in sensors}
            conn.commit()
            continue
        for p in down:
            for bus_id in hh_bus_by_phase[p]:
                loads[bus_id] = 0.0
        update_household_loads(net, feeder, loads)
        sgen_indices = []
        if leo_enabled:
            for p in PHASES:
                kw = energy_kwh[p] / 0.25
                if p not in down and abs(kw) > 1e-9:
                    sgen_indices.append(pp.create_asymmetric_sgen(
                        net, bus=root_idx, **{f"p_{PHASE_LETTER[p]}_mw": kw / 1000.0}))
        run_power_flow(net)
        violations = detect_violations(net, v_limit_pct)
        if sgen_indices:
            net.asymmetric_sgen.drop(sgen_indices, inplace=True)

        for phase in PHASES:
            phase_v = violations[violations["phase"] == phase]
            # Fuse trip is on conductor (line) loading only. The busbar row
            # carries the TRANSFORMER's loading, which runs 150%+ every
            # evening: Indian DTs ride that for hours (thermal time constant,
            # HRC fuses sized above rating) and fail by ageing, not an
            # instant trip. It's reported as an overload event, not a trip.
            line_v = phase_v[phase_v["bus_id"] != str(feeder.root)]
            max_loading = line_v["loading_pct"].max() if not line_v.empty and line_v["loading_pct"].notna().any() else 0.0
            if max_loading is None or (isinstance(max_loading, float) and np.isnan(max_loading)):
                max_loading = 0.0
            if phase not in down and max_loading > TRIP_THRESHOLD_PCT and state.trip_until[phase] is None:
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
            if phase in down:
                continue
            for row in phase_v.itertuples():
                cur.execute(
                    """INSERT INTO network_result (run_id, ts_end, bus_id, phase, voltage_v,
                           loading_pct, violation, is_forecast)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)
                       ON CONFLICT (run_id, ts_end, bus_id, phase, is_forecast) DO NOTHING""",
                    (run_id, ts.to_pydatetime(), row.bus_id, phase, row.voltage_v,
                     None if row.loading_pct is None or pd.isna(row.loading_pct) else float(row.loading_pct),
                     bool(row.violation)),
                )
        prev_v = voltages_now(violations)
        for s in sensors:
            if s.phase in down:
                prev_v[s.id] = None
        conn.commit()
        if i % 24 == 0:
            print(f"  {run_id}: {ts} done ({i+1}/96), trips so far: {n_trips}", flush=True)

    devices.tick(target_ts[-1])  # closes the last interval: the gateway records its dispatch
    devices.gateway_stop()
    cur.close()
    print(f"{run_id}: finished, {n_trips} overload trip(s), {devices.missed_acks} missed gateway ack(s)")
    return {"trips": n_trips, "missed_acks": devices.missed_acks}


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
    run_day() reduces that household's network load: a meter and a
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


def automated_dr(feeder, scenario: dict, truth: pd.DataFrame, plan_date, forecast_total_kw: np.ndarray,
                 weather_15min: pd.DataFrame, net_load: pd.DataFrame, ac_window=None) -> tuple[dict, dict]:
    """Automated DR for one day, same rules as eval/sweep.py: enrolled pumps
    move from 18-19 IST to the midday hour with the most forecast solar
    surplus; on an event day enrolled ACs cycle down in `ac_window`, with
    pre-cooling before and a small rebound after. Returns ({bus_id: 96 kW to
    subtract}, summary for the event log and settlement)."""
    sd = scenario.get("smart_dr", {})
    hh = feeder.household.reset_index(drop=True)
    t = truth.set_index("household_id")
    erng = np.random.default_rng(scenario["sim"]["seed"] + 3)
    smart_pump = hh["id"].map(t["has_pump"]).fillna(False).to_numpy(bool) & (erng.random(len(hh)) < sd.get("pump_relay_share", 0))
    smart_ac = hh["id"].map(t["has_ac"]).fillna(False).to_numpy(bool) & (erng.random(len(hh)) < sd.get("ac_controller_share", 0))
    day_ts = pd.date_range(pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")
    start_h = ((day_ts.hour + day_ts.minute / 60.0 + 5.5 - 0.25) % 24).to_numpy()
    orng = np.random.default_rng(abs(hash(str(plan_date))) % 2**32)
    adjust: dict[str, np.ndarray] = {}
    eve = np.where((start_h >= 18) & (start_h < 19))[0]
    lo, hi = sd.get("shift_window_ist", [10, 15])
    cand = [i for i in range(92) if lo <= start_h[i] and start_h[i + 3] + 0.25 <= hi]
    s0 = min(cand, key=lambda i: forecast_total_kw[i:i + 4].sum())
    pump_homes = []
    for k in np.where(smart_pump)[0]:
        if orng.random() < sd.get("pump_override_prob", 0):
            continue
        a = adjust.setdefault(hh["bus_id"].iat[k], np.zeros(96))
        a[eve] += 0.75
        a[s0:s0 + 4] -= 0.75
        pump_homes.append(hh["id"].iat[k])
    summary = {"pump_homes": pump_homes, "pump_slot_ist": float(start_h[s0]),
               "pump_kwh": 0.75 * len(eve) * 0.25 * len(pump_homes), "ac_homes": [], "ac_kw": 0.0, "ac_kwh": 0.0,
               "ac_payment_rs": sd.get("ac_event_payment_rs", 0), "pump_fee_rs_per_month": None}
    if ac_window is not None:
        temp = weather_15min.set_index("ts")["temperature_c"].reindex(day_ts).ffill().bfill().to_numpy()
        duty = np.clip((temp - 26.0) / 8.0, 0, 1) * ((start_h + 0.25 >= 10) & (start_h + 0.25 <= 23))
        win = np.where((day_ts > ac_window[0]) & (day_ts <= ac_window[1]))[0]
        if len(win):
            w0, w1 = win[0], win[-1] + 1
            for k in np.where(smart_ac)[0]:
                if orng.random() < sd.get("override_prob", 0):
                    continue
                bus = hh["bus_id"].iat[k]
                base = net_load[bus].reindex(day_ts).to_numpy() if bus in net_load.columns else np.zeros(96)
                cut = np.minimum(sd["ac_curtail_frac"] * 1.5 * duty[w0:w1], np.clip(base[w0:w1], 0, None))
                e = float(cut.sum() * 0.25)
                if e <= 0:
                    continue
                a = adjust.setdefault(bus, np.zeros(96))
                a[w0:w1] += cut
                a[max(0, w0 - 8):w0] -= sd["precool_frac"] * e / 2.0
                a[w1:w1 + 8] -= sd["rebound_frac"] * e / 2.0
                summary["ac_homes"].append(hh["id"].iat[k])
                summary["ac_kw"] += float(cut.mean())
                summary["ac_kwh"] += e
    return adjust, summary


def write_auto_dr_events(conn, run_id: str, summary: dict, shift_ts, ac_ts) -> None:
    cur = conn.cursor()
    cur.execute("INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'dr_auto_shift','all','leo',%s)",
                (run_id, shift_ts.to_pydatetime(), psycopg2.extras.Json({k: summary[k] for k in ("pump_homes", "pump_slot_ist", "pump_kwh")})))
    if summary["ac_homes"] and ac_ts is not None:
        cur.execute("INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'dr_auto_ac','all','leo',%s)",
                    (run_id, ac_ts.to_pydatetime(), psycopg2.extras.Json({k: summary[k] for k in ("ac_homes", "ac_kw", "ac_kwh", "ac_payment_rs")})))
    conn.commit()
    cur.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-date", default="2026-04-27", help="the confirmed annual peak-demand day")
    parser.add_argument("--normal-baseline", action=argparse.BooleanOptionalAction, default=True,
                         help="record the 'normal' (LEO) and 'baseline' (no LEO) peak days")
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

    scenario = load_scenario()
    nb = scenario["neighbourhood"]
    rng = np.random.default_rng(scenario["sim"]["seed"])

    print("building feeder and world truth...")
    feeder = build_feeder(scenario)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load, true_pv, net_load, truth = build_true_load(feeder, scenario, weather_15min, rng, return_truth=True)

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

    # The world's side of LEO's devices; every decision below is the gateway's.
    cur = conn.cursor()
    cur.execute("""SELECT pb.household_id, h.phase::text AS phase FROM premise_backup pb
                   JOIN household h ON h.id = pb.household_id ORDER BY pb.household_id""")
    premises = pd.DataFrame(cur.fetchall(), columns=["household_id", "phase"])
    cur.close()
    devices = Field(feeder.sensor, premises, nb["nominal_v_ln"])
    devices.connect()
    seed = scenario["sim"]["seed"]

    def world_rng() -> np.random.Generator:
        """Sensor noise and dropouts: the same draws in every run of the
        day, so runs differ only where LEO's actions (or an outage) do."""
        return np.random.default_rng(seed + 100)

    def record_day(run_id: str, leo: bool, date_, plans, reductions, **kw):
        return run_day(conn, run_id, leo, date_, scenario, feeder, net, net_load, true_load, plans, reductions,
                       devices, world_rng(), **kw)

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

    # Automated DR on the peak day: pumps shifted to midday, AC event in the DR window.
    fc_total = sum(np.array(plans[p]["forecast_p50_kw"]) for p in PHASES)
    auto_adjust, auto_summary = automated_dr(feeder, scenario, truth, plan_date, fc_total, weather_15min, net_load,
                                             ac_window=(window_start, window_end))
    approval_ts = run_time_1400 + pd.Timedelta(hours=4)
    print(f"automated DR: {len(auto_summary['pump_homes'])} pumps -> {auto_summary['pump_slot_ist']:.2f} IST, "
          f"{len(auto_summary['ac_homes'])} ACs cycled ({auto_summary['ac_kw']:.1f} kW)")

    if args.normal_baseline:
        print("\n=== LEO-on run ('normal') ===")
        t0 = time.perf_counter()
        record_day("normal", True, plan_date, approved_plans, accepted_reduction_kw,
            dr_window=(window_start, window_end), load_adjust=auto_adjust)
        write_dr_window_events(conn, "normal", dr_result, window_start, window_end, len(accepted_reduction_kw))
        write_auto_dr_events(conn, "normal", auto_summary, approval_ts, window_start)
        print(f"took {time.perf_counter()-t0:.1f}s")

        print("\n=== baseline run (battery + DR disabled) ===")
        init_run(conn, "baseline", False, scenario["sim"]["seed"], plan_date,
                 "sim/loop.py recorded run (baseline: battery disabled)")
        t0 = time.perf_counter()
        record_day("baseline", False, plan_date, None, {})
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
        init_run(conn, "outage", True, seed, plan_date,
                 f"sim/loop.py recorded run: unplanned upstream fault {outage_start}–{outage_end}")
        copy_dr_rows(conn, "normal", "outage")
        record_day("outage", True, plan_date, approved_plans, accepted_reduction_kw,
            dr_window=(window_start, window_end), load_adjust=auto_adjust,
            outages=(Outage(outage_start, outage_end),))
        write_dr_window_events(conn, "outage", dr_result, window_start, window_end, len(accepted_reduction_kw))
        write_auto_dr_events(conn, "outage", auto_summary, approval_ts, window_start)

    if args.load_shedding:
        # §14 "Load shedding": the DISCOM publishes a schedule at 14:00; the
        # cloud relays it to the gateway, which enters Pre-outage and raises
        # each block's reserve (the gateway records the notice and the mode
        # change); the cloud SMSes households; the cut happens on schedule.
        print("\n=== planned load-shedding run ('load_shedding') ===")
        notice = Notice(pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(hours=14 - 5, minutes=0 - 30),
                        outage_start, outage_end)
        init_run(conn, "load_shedding", True, seed, plan_date,
                 f"sim/loop.py recorded run: DISCOM load-shedding notice {notice.notice_ts}, cut {outage_start}–{outage_end}")
        copy_dr_rows(conn, "normal", "load_shedding")
        record_day("load_shedding", True, plan_date, approved_plans, accepted_reduction_kw,
            dr_window=(window_start, window_end), load_adjust=auto_adjust,
            outages=(Outage(outage_start, outage_end),), notice=notice)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,'alert_sent','all','leo',%s)",
            ("load_shedding", (notice.notice_ts + pd.Timedelta(minutes=5)).to_pydatetime(), psycopg2.extras.Json({
                "n_households": int(len(feeder.household)), "channel": "sms",
                "message": "Scheduled power cut 19:40-21:10 tonight. Please charge phones and home inverters now."})),
        )
        conn.commit()
        cur.close()
        write_dr_window_events(conn, "load_shedding", dr_result, window_start, window_end, len(accepted_reduction_kw))
        write_auto_dr_events(conn, "load_shedding", auto_summary, approval_ts, window_start)

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
        init_run(conn, "surplus", True, seed, surplus_date,
                 "sim/loop.py recorded run: sunny low-demand day, midday PV export")
        persist_forecast(conn, "surplus", surplus_artifacts, nb["dt_id"])
        sfc = sum(np.array(surplus_plans[p]["forecast_p50_kw"]) for p in PHASES)
        s_adjust, s_summary = automated_dr(feeder, scenario, truth, surplus_date, sfc, weather_15min, net_load)
        record_day("surplus", True, surplus_date, surplus_approved, {}, load_adjust=s_adjust, forecast_run_id="surplus")
        write_auto_dr_events(conn, "surplus", s_summary, surplus_run_time + pd.Timedelta(hours=4), None)
        init_run(conn, "surplus_baseline", False, seed, surplus_date,
                 "sim/loop.py recorded run: sunny low-demand day, battery disabled")
        record_day("surplus_baseline", False, surplus_date, None, {}, forecast_run_id="surplus")

    devices.close()
    conn.close()


if __name__ == "__main__":
    main()
