"""gateway/service.py — the LEO edge gateway (Architecture: C4, C6, C7, C9).

The process that would run on the Raspberry Pi-class box at the battery
site. It needs no internet: everything it uses arrives from LEO's own
devices or was put in the local store by the day-ahead job.

  in   MQTT   leo/sensor/{devEUI}/up      voltage uplinks (+ busbar CT)       -> C4 ingest
       MQTT   leo/sensor/{devEUI}/event   power-fail / restore, immediate     -> C9
       MQTT   leo/clock/tick              time; a 15-min boundary runs C7
       Modbus inverters 1-3               SoC, power actually flowed, grid state
       Modbus premise meters              backup energy and current per premise
       REST   POST /notices/load_shedding DISCOM schedule (relayed by the cloud)
       DB     plan (approved), forecast   the day-ahead plan and its P50
  out  Modbus inverters                   mode, setpoint, watchdog
       Modbus premise meters              backup current limits
       DB     sensor_reading, battery_telemetry, dispatch, mode_transition,
              event, premise_meter
       MQTT   leo/gateway/ack, leo/gateway/heartbeat

Only an APPROVED plan is executed: a run whose plan has not been approved
in the operator console is refused, and the inverters stay idle.

A run is started with POST /runs/start. `control: false` is shadow mode
(roadmap phase 1, and the no-LEO baseline): ingest and record, command
nothing.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import paho.mqtt.client as mqtt
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from gateway.controller import PHASES, Controller, TickOutput
from gateway.devices import DeviceError, Inverters, PremiseMeters
from gateway.ingest import IngestError, Ingestor
from gateway.registry import Registry, load_registry

log = logging.getLogger("leo.gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

GATEWAY_ID = os.environ.get("GATEWAY_ID", "gw-01")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
INVERTER_HOST = os.environ.get("INVERTER_HOST", "localhost")
INVERTER_PORT = int(os.environ.get("INVERTER_PORT", "5020"))
METER_HOST = os.environ.get("METER_HOST", "localhost")
METER_PORT = int(os.environ.get("METER_PORT", "5021"))
# Inverters fall back to idle if the gateway goes quiet: two control
# intervals, so one lost write keeps the last setpoint, a dead gateway
# leaves the blocks safe ("run their last plan, then safe default").
WATCHDOG_S = int(os.environ.get("INVERTER_WATCHDOG_S", "1800"))

TOPIC_UPLINK = "leo/sensor/+/up"
TOPIC_EVENT = "leo/sensor/+/event"
TOPIC_TICK = "leo/clock/tick"
TOPIC_ACK = "leo/gateway/ack"
TOPIC_HEARTBEAT = "leo/gateway/heartbeat"

INTERVAL = timedelta(minutes=15)


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def is_boundary(ts: datetime) -> bool:
    return ts.second == 0 and ts.microsecond == 0 and ts.minute % 15 == 0


class PlanNotApproved(RuntimeError):
    pass


@dataclass
class Run:
    run_id: str
    plan_date: date_cls
    control: bool
    day_start: datetime
    day_end: datetime
    registry: Registry
    controller: Optional[Controller] = None
    plan_kw: dict = field(default_factory=dict)
    inverters: Optional[Inverters] = None
    meters: Optional[PremiseMeters] = None
    last_tick: Optional[datetime] = None
    pending: dict = field(default_factory=dict)        # phase -> (planned_kw, Decision)
    energy_kwh: dict = field(default_factory=dict)     # phase -> grid-port kWh this interval
    islanded_in_interval: set = field(default_factory=set)
    meter_kwh: dict = field(default_factory=dict)      # household -> cumulative kWh at last read
    soc: dict = field(default_factory=dict)


class Gateway:
    def __init__(self) -> None:
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.run: Optional[Run] = None
        self.ingestor: Optional[Ingestor] = None
        self.conn = None
        self.mqtt: Optional[mqtt.Client] = None
        self.mqtt_connected = False
        self.ticks_seen = 0
        self.last_error: Optional[str] = None
        self._worker = threading.Thread(target=self._work, name="gateway-worker", daemon=True)

    # ---- plumbing ---------------------------------------------------------

    def start(self) -> None:
        self._worker.start()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"leo-{GATEWAY_ID}")
        client.on_connect = self._on_connect
        client.on_disconnect = lambda *a, **k: setattr(self, "mqtt_connected", False)
        client.on_message = self._on_message
        client.reconnect_delay_set(1, 10)
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        self.mqtt = client

    def stop(self) -> None:
        if self.mqtt is not None:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.mqtt_connected = True
        client.subscribe([(TOPIC_UPLINK, 1), (TOPIC_EVENT, 1), (TOPIC_TICK, 1)])
        log.info("MQTT connected to %s:%s", MQTT_HOST, MQTT_PORT)

    def _on_message(self, client, userdata, msg) -> None:
        # Never work in paho's network thread: queue it, in arrival order,
        # behind anything already waiting.
        if msg.topic == TOPIC_TICK:
            self.submit(self._on_tick, msg.payload)
        elif msg.topic.endswith("/up"):
            self.submit(self._on_uplink, msg.topic, msg.payload)
        elif msg.topic.endswith("/event"):
            self.submit(self._on_event, msg.topic, msg.payload)

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        self.jobs.put((fn, args, fut))
        return fut

    def call(self, fn, *args, timeout: float = 60.0):
        return self.submit(fn, *args).result(timeout=timeout)

    def _work(self) -> None:
        while True:
            fn, args, fut = self.jobs.get()
            try:
                fut.set_result(fn(*args))
            except Exception as exc:  # one bad message must not stop the gateway
                if not isinstance(exc, (IngestError, PlanNotApproved, LookupError)):
                    log.exception("gateway job %s failed", getattr(fn, "__name__", fn))
                self.last_error = f"{type(exc).__name__}: {exc}"
                fut.set_exception(exc)
                if self.conn is not None:
                    try:
                        self.conn.rollback()
                    except Exception:
                        self.conn = None

    def _db(self):
        if self.conn is None or self.conn.closed:
            self.conn = db_connect()
        return self.conn

    def _publish(self, topic: str, payload: dict) -> None:
        if self.mqtt is not None:
            self.mqtt.publish(topic, json.dumps(payload, default=str), qos=1)

    # ---- runs -------------------------------------------------------------

    def _start_run(self, run_id: str, plan_date: date_cls, plan_run_id: Optional[str],
                   forecast_run_id: Optional[str], control: bool) -> dict:
        conn = self._db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM run WHERE run_id = %s", (run_id,))
        if cur.fetchone() is None:
            raise LookupError(f"run {run_id!r} is not registered")
        registry = load_registry(conn)
        self.ingestor = Ingestor(registry.sensors)
        day_start = datetime.combine(plan_date, datetime.min.time(), tzinfo=timezone.utc)
        run = Run(run_id=run_id, plan_date=plan_date, control=control, day_start=day_start,
                  day_end=day_start + timedelta(days=1), registry=registry)
        summary = {"run_id": run_id, "plan_date": plan_date.isoformat(), "control": control}
        if control:
            plan_run_id = plan_run_id or run_id
            cur.execute("""SELECT phase::text, ts_end, setpoint_kw, approved_at, approved_by FROM plan
                           WHERE run_id = %s AND plan_date = %s ORDER BY phase, ts_end""", (plan_run_id, plan_date))
            rows = cur.fetchall()
            for p in PHASES:
                ph = [r for r in rows if r[0] == p]
                if len(ph) != 96:
                    raise PlanNotApproved(f"no complete plan for phase {p} on {plan_date} under run {plan_run_id!r}")
                if any(r[3] is None for r in ph):
                    raise PlanNotApproved(f"the plan for phase {p} on {plan_date} has not been approved")
                run.plan_kw[p] = np.array([r[2] for r in ph])
            cur.execute("""SELECT phase::text, ts_end, p50_kw FROM forecast
                           WHERE run_id = %s AND model = 'net_load' AND ts_end > %s AND ts_end <= %s
                           ORDER BY phase, ts_end""", (forecast_run_id or plan_run_id, day_start, run.day_end))
            frows = cur.fetchall()
            p50 = {p: np.array([r[2] for r in frows if r[0] == p]) for p in PHASES}
            if any(len(v) != 96 for v in p50.values()):
                raise LookupError(f"no day-ahead forecast for {plan_date} under run {forecast_run_id or plan_run_id!r}")
            dvdp = registry.voltage_sensitivity()
            run.controller = Controller(registry.blocks, registry.sensors, registry.premises,
                                        registry.nominal_v_ln, registry.v_limit_pct, dvdp)
            run.controller.start_day(plan_date, p50)
            run.inverters = Inverters(INVERTER_HOST, INVERTER_PORT, registry.unit_by_phase)
            run.meters = PremiseMeters(METER_HOST, METER_PORT, registry.meter_unit_by_household)
            for h in registry.premises["household_id"]:
                run.meters.set_limit(h, 0.0)
                run.meter_kwh[h] = run.meters.read_meter(h).backup_kwh
            for p in PHASES:
                run.inverters.idle(p)
            summary.update({"approved_by": rows[0][4], "dvdp_pu_per_kw": dvdp,
                            "planned_discharge_kwh": {p: float(np.clip(run.plan_kw[p], 0, None).sum() * 0.25) for p in PHASES}})
        cur.close()
        self.run = run
        log.info("run %s started (%s, %s)", run_id, plan_date, "control" if control else "shadow")
        return summary

    def _stop_run(self) -> dict:
        run, self.run = self.run, None
        if run is None:
            return {"stopped": None}
        if run.control:
            for p in PHASES:
                try:
                    run.inverters.idle(p)
                except DeviceError as exc:
                    log.warning("could not idle %s on stop: %s", p, exc)
            run.inverters.close()
            run.meters.close()
        log.info("run %s stopped", run.run_id)
        return {"stopped": run.run_id}

    def _notice(self, notice_ts: datetime, start: datetime, end: datetime, reason: str) -> dict:
        run = self._require_control()
        out = run.controller.announce_load_shedding(notice_ts, start, end, reason)
        self._persist(run, out)
        self._db().commit()
        return {"mode": run.controller.mode.mode, "reserve_soc": run.controller.notice.reserve_soc}

    def _require_control(self) -> Run:
        if self.run is None or not self.run.control:
            raise LookupError("no run under LEO control")
        return self.run

    # ---- ingestion (C4) ---------------------------------------------------

    def _on_uplink(self, topic: str, payload: bytes) -> None:
        if self.ingestor is None:
            return
        up = self.ingestor.uplink(topic, payload)
        run = self.run
        if up is None or run is None:
            return
        if run.controller is not None:
            run.controller.set_supply(up.sensor_id, up.supply_present)
        if run.day_start <= up.batch_end <= run.day_end:
            cur = self._db().cursor()
            cur.execute("""INSERT INTO sensor_reading (run_id, sensor_id, ts_end, voltage_v, supply_present, provenance)
                           VALUES (%s,%s,%s,%s,%s,'measured') ON CONFLICT DO NOTHING""",
                        (run.run_id, up.sensor_id, up.batch_end, up.mean_voltage_v, up.supply_present))
            self.conn.commit()

    def _on_event(self, topic: str, payload: bytes) -> None:
        if self.ingestor is None:
            return
        ev = self.ingestor.event(topic, payload)
        run = self.run
        if run is None:
            return
        if run.controller is not None:
            run.controller.set_supply(ev.sensor_id, ev.kind == "power_restore")
        if ev.kind == "power_fail" and run.day_start <= ev.ts <= run.day_end:
            cur = self._db().cursor()
            cur.execute("""INSERT INTO event (run_id, ts, kind, scope, source, payload)
                           VALUES (%s,%s,'power_fail',%s,'sensor',%s)""",
                        (run.run_id, ev.ts, f"phase:{ev.phase}", psycopg2.extras.Json(
                            {"sensor_id": ev.sensor_id, "placement": ev.placement, "last_voltage_v": ev.last_voltage_v})))
            self.conn.commit()

    # ---- the control tick (C7 at boundaries, C9 always) -------------------

    def _on_tick(self, payload: bytes) -> None:
        try:
            ts = datetime.fromisoformat(payload.decode().strip().strip('"').replace("Z", "+00:00"))
        except ValueError:
            log.warning("unparseable tick %r", payload)
            return
        self.ticks_seen += 1
        run = self.run
        if run is None or not (run.day_start <= ts <= run.day_end):
            return  # free-running clock outside any run, or a stale tick
        if run.last_tick is not None and ts < run.last_tick:
            return
        if run.last_tick is not None and ts == run.last_tick:
            self._ack(run, ts)  # a repeat of a tick already handled
            return
        seg_h = (ts - run.last_tick).total_seconds() / 3600.0 if run.last_tick else 0.0
        if run.control:
            self._control_tick(run, ts, seg_h)
        run.last_tick = ts
        self._db().commit()
        self._ack(run, ts)

    def _ack(self, run: Run, ts: datetime) -> None:
        mode = run.controller.mode.mode if run.controller else "shadow"
        self._publish(TOPIC_ACK, {"tick": ts.isoformat(), "run_id": run.run_id, "mode": mode})
        self._publish(TOPIC_HEARTBEAT, {"gateway_id": GATEWAY_ID, "ts": ts.isoformat(), "run_id": run.run_id,
                                        "mode": mode})

    def _control_tick(self, run: Run, ts: datetime, seg_h: float) -> None:
        ctl, cur = run.controller, self._db().cursor()
        try:
            tel = {p: run.inverters.read_telemetry(p) for p in PHASES}
        except DeviceError as exc:
            # Blind this tick: command nothing; the inverters ride their
            # watchdog (last setpoint, then idle).
            log.error("inverter telemetry unavailable at %s: %s", ts, exc)
            return
        for p in PHASES:
            t = tel[p]
            run.soc[p] = t.soc
            run.energy_kwh[p] = run.energy_kwh.get(p, 0.0) + t.power_kw * seg_h
            cur.execute("""INSERT INTO battery_telemetry (run_id, block_id, ts, soc, power_kw, voltage_v, grid_present, alarm_bits)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (run.run_id, f"BATT-{p}", ts, t.soc, t.power_kw, t.voltage_v, t.grid_present, t.alarm_bits))
        run.islanded_in_interval |= set(ctl.islanded_phases())

        boundary = is_boundary(ts)
        if boundary and run.pending:
            self._finalize_interval(run, ts, cur)
        if boundary:
            self._read_meters(run, ts, cur)

        out = ctl.on_tick(ts, run.soc)
        self._persist(run, out)
        run.islanded_in_interval |= set(ctl.islanded_phases())

        i = ctl.interval_index(ts) if boundary else None
        if i is not None:
            ing, nom = self.ingestor, run.registry.nominal_v_ln
            decisions = ctl.decide(
                i, load_kw={p: ing.phase_load_kw(p, ts) for p in PHASES},
                busbar_v_pu={p: ing.voltage_pu("busbar", p, nom) for p in PHASES},
                far_end_v_pu={p: ing.voltage_pu("far_end", p, nom) for p in PHASES},
                soc=run.soc)
            for p, d in decisions.items():
                if d.mode != "backup":
                    try:
                        run.inverters.command(p, d.setpoint_kw, WATCHDOG_S)
                    except DeviceError as exc:
                        log.error("setpoint to %s failed: %s", p, exc)
                run.pending[p] = (float(run.plan_kw[p][i]), d)
            run.energy_kwh = {p: 0.0 for p in PHASES}
            run.islanded_in_interval = set(ctl.islanded_phases())
        cur.close()

    def _finalize_interval(self, run: Run, ts_end: datetime, cur) -> None:
        """The dispatch row for the interval that just ended: what was
        planned, what actually flowed (from the inverter's own telemetry),
        the SoC it ended on, and why."""
        for p, (planned_kw, d) in run.pending.items():
            mode = "backup" if p in run.islanded_in_interval else d.mode
            cur.execute("""INSERT INTO dispatch (run_id, block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (run.run_id, f"BATT-{p}", ts_end, planned_kw, run.energy_kwh.get(p, 0.0) / 0.25,
                         run.soc[p], mode, None if mode == "backup" else d.rule))
        run.pending = {}

    def _read_meters(self, run: Run, ts_end: datetime, cur) -> None:
        backup_live = bool(run.islanded_in_interval)
        for h in run.registry.premises["household_id"]:
            try:
                m = run.meters.read_meter(h)
            except DeviceError as exc:
                log.warning("premise meter %s unreadable: %s", h, exc)
                continue
            delta = max(0.0, m.backup_kwh - run.meter_kwh.get(h, m.backup_kwh))
            run.meter_kwh[h] = m.backup_kwh
            if backup_live or delta > 1e-6 or m.limit_active:
                cur.execute("""INSERT INTO premise_meter (run_id, household_id, ts_end, backup_kwh, current_a, limit_active)
                               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (run.run_id, h, ts_end, delta, m.current_a, m.limit_active))

    def _persist(self, run: Run, out: TickOutput) -> None:
        cur = self._db().cursor()
        for e in out.events:
            cur.execute("INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,%s,%s,%s,%s)",
                        (run.run_id, e.ts, e.kind, e.scope, e.source, psycopg2.extras.Json(e.payload, dumps=lambda o: json.dumps(o, default=str))))
        for tr in out.transitions:
            ts = tr.ts
            cur.execute("SELECT 1 FROM mode_transition WHERE run_id = %s AND ts = %s", (run.run_id, ts))
            while cur.fetchone() is not None:  # two transitions in one instant: keep both, in order
                ts += timedelta(seconds=1)
                cur.execute("SELECT 1 FROM mode_transition WHERE run_id = %s AND ts = %s", (run.run_id, ts))
            cur.execute("INSERT INTO mode_transition (run_id, ts, from_mode, to_mode, owner, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                        (run.run_id, ts, tr.from_mode, tr.to_mode, tr.owner, tr.reason))
        if out.premise_limits_a is not None and run.meters is not None:
            for h, amps in out.premise_limits_a.items():
                try:
                    run.meters.set_limit(h, amps)
                except DeviceError as exc:
                    log.error("backup limit for %s failed: %s", h, exc)
        cur.close()

    # ---- introspection ----------------------------------------------------

    def snapshot(self) -> dict:
        run = self.run
        ing = self.ingestor
        return {
            "gateway_id": GATEWAY_ID, "mqtt_connected": self.mqtt_connected, "ticks_seen": self.ticks_seen,
            "run": None if run is None else {
                "run_id": run.run_id, "plan_date": run.plan_date.isoformat(), "control": run.control,
                "last_tick": run.last_tick.isoformat() if run.last_tick else None,
                "mode": run.controller.mode.mode if run.controller else "shadow",
                "soc": run.soc, "islanded": run.controller.islanded_phases() if run.controller else [],
            },
            "ingest": None if ing is None else {
                "accepted": ing.accepted, "rejected": dict(ing.rejected),
                "latest": {s: {"batch_end": u.batch_end.isoformat(), "supply_present": u.supply_present,
                               "voltage_v": u.last_voltage_v, "active_power_kw": u.active_power_kw}
                           for s, u in ing.latest.items()},
            },
            "last_error": self.last_error,
        }


# ---- HTTP surface -----------------------------------------------------------

gateway = Gateway()


@asynccontextmanager
async def lifespan(app: FastAPI):
    gateway.start()
    yield
    gateway.stop()


app = FastAPI(title="leo-gateway", lifespan=lifespan)


class StartRun(BaseModel):
    run_id: str
    plan_date: date_cls
    plan_run_id: Optional[str] = None       # where the approved plan lives (default: run_id)
    forecast_run_id: Optional[str] = None   # where its P50 forecast lives (default: plan_run_id)
    control: bool = True                    # False = shadow mode: ingest and record only


class LoadSheddingNotice(BaseModel):
    notice_ts: datetime
    start: datetime
    end: datetime
    reason: str = ""


def _call(fn, *args):
    try:
        return gateway.call(fn, *args)
    except PlanNotApproved as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DeviceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gateway", "mqtt_connected": gateway.mqtt_connected,
            "run": gateway.run.run_id if gateway.run else None}


@app.get("/state")
def state() -> dict:
    return gateway.snapshot()


@app.post("/runs/start")
def start_run(req: StartRun) -> dict:
    return _call(gateway._start_run, req.run_id, req.plan_date, req.plan_run_id, req.forecast_run_id, req.control)


@app.post("/runs/stop")
def stop_run() -> dict:
    return _call(gateway._stop_run)


@app.post("/notices/load_shedding")
def load_shedding_notice(req: LoadSheddingNotice) -> dict:
    return _call(gateway._notice, req.notice_ts, req.start, req.end, req.reason)
