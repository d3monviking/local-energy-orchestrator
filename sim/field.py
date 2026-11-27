"""sim/field.py — the simulated world's side of LEO's device interfaces.

sim/loop.py owns the truth (household loads, the power flow, outages).
Everything LEO learns about that truth, and everything LEO does about it,
crosses one of the interfaces a real deployment has, through this file:

  sensors      ChirpStack-shaped uplinks and power-fail/restore events on
               MQTT (contracts/mqtt_topics.md), with sensor noise and
               dropouts from sim/measure.py
  inverters    the Modbus mock (mocks/modbus_battery.py): the world sizes
               the packs, sets the grid at their terminals and the backup
               draw, and advances their physics once the gateway has acted
               — through the mock's simulation hooks, never the gateway's
               registers
  meters       the backup premise meters: the world writes what each
               premise actually drew through the limit the gateway set
  clock        the clock service: paused, then jumped interval by interval
  gateway      REST to start/stop a run and to relay a DISCOM notice (what
               the cloud does in the field); MQTT ack per tick

Lockstep: publish the sensors' view of time T, jump the clock to T, wait
for the gateway's ack of T (it has read its devices and written its
setpoints), then advance the inverters to the next event. If the gateway
does not answer, the world carries on and the inverters ride their own
watchdog, exactly as they would in the field.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

import numpy as np
import paho.mqtt.client as mqtt
import pandas as pd
import requests
from pymodbus.client import ModbusTcpClient

log = logging.getLogger("leo.sim.field")

UNIT_BY_PHASE = {"R": 1, "Y": 2, "B": 3}
# Simulation hooks on mocks/modbus_battery.py (not the device contract).
SIM_STEPPED, SIM_CAP, SIM_POWER, SIM_SOC, SIM_GRID, SIM_BACKUP_W, SIM_ADVANCE = range(40100, 40107)
REG_MODE, REG_SETPOINT = 40010, 40011


def _u16(value) -> int:
    return int(round(value)) & 0xFFFF


def _s16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


class GatewayRefused(RuntimeError):
    pass


class Field:
    def __init__(self, sensors: pd.DataFrame, premises: pd.DataFrame, nominal_v_ln: float,
                 gateway_id: str = "gw-01", ack_timeout_s: float = 30.0):
        self.sensors = sensors.reset_index(drop=True)
        self.premises = premises.reset_index(drop=True)        # household_id, phase; household_id order
        self.meter_unit = {h: k for k, h in enumerate(self.premises["household_id"], start=1)}
        self.nominal_v_ln = nominal_v_ln
        self.gateway_id = gateway_id
        self.ack_timeout_s = ack_timeout_s
        self.mqtt_host = os.environ.get("MQTT_HOST", "localhost")
        self.mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
        self.clock_url = os.environ.get("CLOCK_URL", "http://localhost:8010")
        self.gateway_url = os.environ.get("GATEWAY_URL", "http://localhost:8020")
        self.inv = ModbusTcpClient(os.environ.get("MODBUS_HOST", "localhost"),
                                   port=int(os.environ.get("MODBUS_PORT", "5020")), timeout=3)
        self.met = ModbusTcpClient(os.environ.get("METER_HOST", os.environ.get("MODBUS_HOST", "localhost")),
                                   port=int(os.environ.get("METER_PORT", "5021")), timeout=3)
        self.fcnt = {s: 0 for s in self.sensors["id"]}
        self._acks: set = set()
        self._ack_cv = threading.Condition()
        self._pending = []
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="leo-sim-field")
        self.mqtt.on_message = self._on_ack
        self.missed_acks = 0

    # ---- connections ------------------------------------------------------

    def connect(self) -> None:
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        self.mqtt.subscribe("leo/gateway/ack", qos=1)
        self.mqtt.loop_start()
        for name, client in (("inverters", self.inv), ("premise meters", self.met)):
            if not client.connect():
                raise ConnectionError(f"cannot reach the {name} Modbus server")
        requests.get(f"{self.gateway_url}/health", timeout=5).raise_for_status()

    def close(self) -> None:
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        self.inv.close()
        self.met.close()

    def _on_ack(self, client, userdata, msg) -> None:
        try:
            tick = datetime.fromisoformat(json.loads(msg.payload)["tick"])
        except (ValueError, KeyError, TypeError):
            return
        with self._ack_cv:
            self._acks.add(tick)
            self._ack_cv.notify_all()

    def _inv_write(self, phase: str, address: int, value) -> None:
        rr = self.inv.write_register(address=address, value=_u16(value), device_id=UNIT_BY_PHASE[phase])
        if rr.isError():
            raise RuntimeError(f"inverter {phase} write {address}: {rr}")

    def _met_write(self, household_id: str, address: int, value) -> None:
        rr = self.met.write_register(address=address, value=_u16(value), device_id=self.meter_unit[household_id])
        if rr.isError():
            raise RuntimeError(f"meter {household_id} write {address}: {rr}")

    # ---- run setup --------------------------------------------------------

    def install(self, blocks: dict[str, tuple[float, float]], soc0: float = 0.5) -> None:
        """Fresh packs for a run: stepped physics, sized per scenario, idle,
        grid present, no backup draw; premise meters zeroed."""
        for p, (cap_kwh, power_kw) in blocks.items():
            for reg, val in ((SIM_STEPPED, 1), (SIM_CAP, cap_kwh * 10), (SIM_POWER, power_kw * 10),
                             (SIM_GRID, 1), (SIM_BACKUP_W, 0), (REG_SETPOINT, 0), (REG_MODE, 0),
                             (SIM_SOC, soc0 * 1000)):
                self._inv_write(p, reg, val)
        for h in self.premises["household_id"]:
            for reg in (30000, 30001, 30002, 30010):
                self._met_write(h, reg, 0)

    def pause_clock(self) -> None:
        requests.post(f"{self.clock_url}/pause", json={"paused": True}, timeout=5).raise_for_status()

    def gateway_start(self, run_id: str, plan_date, control: bool, plan_run_id: str | None = None,
                      forecast_run_id: str | None = None) -> dict:
        r = requests.post(f"{self.gateway_url}/runs/start", timeout=120, json={
            "run_id": run_id, "plan_date": str(plan_date), "control": control,
            "plan_run_id": plan_run_id, "forecast_run_id": forecast_run_id})
        if r.status_code != 200:
            raise GatewayRefused(f"gateway refused run {run_id}: {r.status_code} {r.text}")
        return r.json()

    def gateway_stop(self) -> None:
        requests.post(f"{self.gateway_url}/runs/stop", timeout=30).raise_for_status()

    def relay_notice(self, notice_ts, start, end, reason: str) -> dict:
        """The cloud relays a DISCOM load-shedding schedule to the gateway."""
        r = requests.post(f"{self.gateway_url}/notices/load_shedding", timeout=30, json={
            "notice_ts": pd.Timestamp(notice_ts).isoformat(), "start": pd.Timestamp(start).isoformat(),
            "end": pd.Timestamp(end).isoformat(), "reason": reason})
        r.raise_for_status()
        return r.json()

    # ---- sensors ----------------------------------------------------------

    def _publish(self, topic: str, msg: dict) -> None:
        self._pending.append(self.mqtt.publish(topic, json.dumps(msg), qos=1))

    def uplink(self, sensor, ts: pd.Timestamp, voltage_v: float | None, supply_present: bool,
               ct_kw: float | None = None) -> None:
        self.fcnt[sensor.id] += 1
        obj = {"interval_s": 900, "batch_end": ts.isoformat(), "supply_present": supply_present,
               "voltages_v": [round(voltage_v, 2)] if (supply_present and voltage_v is not None) else []}
        if ct_kw is not None and sensor.placement == "busbar":
            obj["active_power_kw"] = round(ct_kw, 3)
        self._publish(f"leo/sensor/{sensor.dev_eui}/up", {
            "devEUI": sensor.dev_eui, "fCnt": self.fcnt[sensor.id], "fPort": 2,
            "rxInfo": [{"gatewayID": self.gateway_id, "rssi": -97, "loRaSNR": 7.5, "time": ts.isoformat()}],
            "object": obj})

    def power_event(self, sensor, ts: pd.Timestamp, kind: str, last_voltage_v: float | None) -> None:
        self._publish(f"leo/sensor/{sensor.dev_eui}/event", {
            "devEUI": sensor.dev_eui,
            "object": {"event": kind, "ts": ts.isoformat(),
                       "last_voltage_v": None if last_voltage_v is None else round(last_voltage_v, 1)}})

    # ---- time -------------------------------------------------------------

    def tick(self, ts: pd.Timestamp) -> bool:
        """Deliver everything published so far, move the clock to `ts`, and
        wait for the gateway to have acted on it."""
        for info in self._pending:
            info.wait_for_publish(timeout=10)
        self._pending = []
        target = ts.to_pydatetime()
        requests.post(f"{self.clock_url}/jump", json={"to": target.isoformat()}, timeout=10).raise_for_status()
        with self._ack_cv:
            ok = self._ack_cv.wait_for(lambda: target in self._acks, timeout=self.ack_timeout_s)
            self._acks.discard(target)
        if not ok:
            self.missed_acks += 1
            log.warning("gateway did not ack %s; the inverters run on their watchdog", ts)
        return ok

    # ---- inverter physics -------------------------------------------------

    def set_grid(self, phase: str, present: bool) -> None:
        self._inv_write(phase, SIM_GRID, 1 if present else 0)

    def set_backup_demand(self, phase: str, kw: float) -> None:
        self._inv_write(phase, SIM_BACKUP_W, max(0.0, kw) * 1000)

    def advance(self, phases, seconds: float) -> dict[str, dict]:
        out = {}
        for p in phases:
            self._inv_write(p, SIM_ADVANCE, seconds)
            rr = self.inv.read_holding_registers(address=40000, count=6, device_id=UNIT_BY_PHASE[p])
            if rr.isError():
                raise RuntimeError(f"inverter {p} read: {rr}")
            r = rr.registers
            out[p] = {"soc": r[0] / 1000.0, "power_kw": _s16(r[1]) / 1000.0, "backup_kw": r[5] / 1000.0}
        return out

    # ---- premise meters ---------------------------------------------------

    def premise_limit_kw(self, household_id: str) -> float:
        rr = self.met.read_holding_registers(address=30010, count=1, device_id=self.meter_unit[household_id])
        if rr.isError():
            raise RuntimeError(f"meter {household_id} read: {rr}")
        return rr.registers[0] / 10.0 * self.nominal_v_ln / 1000.0

    def meter_record(self, household_id: str, cumulative_kwh: float, current_a: float, limit_active: bool) -> None:
        self._met_write(household_id, 30000, cumulative_kwh * 100)
        self._met_write(household_id, 30001, current_a * 10)
        self._met_write(household_id, 30002, 1 if limit_active else 0)


def ct_reading(true_kw: float, rng: np.random.Generator, noise_frac: float = 0.01) -> float:
    """A split-core CT on the busbar sensor: phase feeder power with ~1% error."""
    return float(true_kw * (1.0 + rng.normal(0.0, noise_frac)))
