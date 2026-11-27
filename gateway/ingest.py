"""gateway/ingest.py — C4 ingestion: sensor uplinks and power events in,
validated and de-duplicated, latest state out.

Messages arrive on MQTT in ChirpStack's envelope (contracts/mqtt_topics.md),
so the same code takes a real LoRaWAN network server's output. Every
message is checked before LEO acts on it: the device must be registered,
the topic and payload must name the same device, timestamps must parse,
values must be physically plausible, and a repeat of an already-seen
(device, interval end) is dropped. Rejections are counted by reason and
exposed on the gateway's /state, never silently swallowed.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

V_PLAUSIBLE = (0.0, 400.0)       # V phase-to-neutral; anything outside is a broken sensor
KW_PLAUSIBLE = (-500.0, 500.0)   # busbar CT on a 100 kVA DT


class IngestError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass
class Uplink:
    sensor_id: str
    phase: str
    placement: str
    batch_end: datetime
    voltages_v: list
    supply_present: bool
    active_power_kw: Optional[float]   # busbar sensors' CT: phase feeder power at batch_end

    @property
    def mean_voltage_v(self) -> Optional[float]:
        return sum(self.voltages_v) / len(self.voltages_v) if self.voltages_v else None

    @property
    def last_voltage_v(self) -> Optional[float]:
        return self.voltages_v[-1] if self.voltages_v else None


@dataclass
class PowerEvent:
    sensor_id: str
    phase: str
    placement: str
    kind: str                          # 'power_fail' | 'power_restore'
    ts: datetime
    last_voltage_v: Optional[float]


def _ts(value, field: str) -> datetime:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError("bad_timestamp", f"{field}={value!r}") from exc
    if ts.tzinfo is None:
        raise IngestError("bad_timestamp", f"{field} has no timezone")
    return ts


def _number(value, lo: float, hi: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise IngestError("bad_value", f"{field}={value!r}")
    if not lo <= value <= hi:
        raise IngestError("implausible_value", f"{field}={value}")
    return float(value)


class Ingestor:
    def __init__(self, sensors: pd.DataFrame):
        self.by_eui = {r.dev_eui: r for r in sensors.itertuples()}
        self.sensor_at = {(r.placement, r.phase): r.id for r in sensors.itertuples()}
        self.latest: dict[str, Uplink] = {}
        self.last_good_v: dict[str, float] = {}
        self.last_seen: dict[str, datetime] = {}
        self.rejected: Counter = Counter()
        self.accepted = 0

    def _envelope(self, topic: str, payload: bytes):
        try:
            msg = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise IngestError("bad_json") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("object"), dict):
            raise IngestError("bad_envelope")
        eui = msg.get("devEUI")
        parts = topic.split("/")
        if len(parts) != 4 or parts[2] != eui:
            raise IngestError("topic_mismatch", f"{topic} vs devEUI {eui}")
        sensor = self.by_eui.get(eui)
        if sensor is None:
            raise IngestError("unknown_device", str(eui))
        return msg, msg["object"], sensor

    def uplink(self, topic: str, payload: bytes) -> Optional[Uplink]:
        """Parsed uplink, or None for a duplicate. Raises IngestError."""
        try:
            _, obj, s = self._envelope(topic, payload)
            batch_end = _ts(obj.get("batch_end"), "batch_end")
            supply = obj.get("supply_present")
            if not isinstance(supply, bool):
                raise IngestError("bad_value", f"supply_present={supply!r}")
            volts = obj.get("voltages_v", [])
            if not isinstance(volts, list):
                raise IngestError("bad_value", "voltages_v is not a list")
            volts = [_number(v, *V_PLAUSIBLE, "voltages_v") for v in volts]
            kw = obj.get("active_power_kw")
            kw = None if kw is None else _number(kw, *KW_PLAUSIBLE, "active_power_kw")
            if kw is not None and s.placement != "busbar":
                raise IngestError("unexpected_field", "active_power_kw from a far-end sensor")
        except IngestError as exc:
            self.rejected[exc.reason] += 1
            raise
        if self.last_seen.get(s.id) is not None and batch_end <= self.last_seen[s.id]:
            self.rejected["duplicate_or_stale"] += 1
            return None
        self.last_seen[s.id] = batch_end
        up = Uplink(s.id, s.phase, s.placement, batch_end, volts, supply, kw)
        self.latest[s.id] = up
        if volts:
            self.last_good_v[s.id] = volts[-1]
        self.accepted += 1
        return up

    def event(self, topic: str, payload: bytes) -> PowerEvent:
        try:
            _, obj, s = self._envelope(topic, payload)
            kind = obj.get("event")
            if kind not in ("power_fail", "power_restore"):
                raise IngestError("bad_value", f"event={kind!r}")
            ts = _ts(obj.get("ts"), "ts")
            lv = obj.get("last_voltage_v")
            lv = None if lv is None else _number(lv, *V_PLAUSIBLE, "last_voltage_v")
        except IngestError as exc:
            self.rejected[exc.reason] += 1
            raise
        self.accepted += 1
        return PowerEvent(s.id, s.phase, s.placement, kind, ts, lv)

    def reset(self) -> None:
        """New run: forget readings from the previous one (time may go backwards)."""
        self.latest.clear()
        self.last_seen.clear()
        self.last_good_v.clear()

    # ---- what C7 gets to see ---------------------------------------------

    def phase_load_kw(self, phase: str, at: datetime) -> Optional[float]:
        """The busbar CT reading for `phase` taken at `at`, or None if that
        uplink never arrived (the controller then uses the forecast)."""
        up = self.latest.get(self.sensor_at.get(("busbar", phase)))
        if up is None or up.batch_end != at or not up.supply_present:
            return None
        return up.active_power_kw

    def voltage_pu(self, placement: str, phase: str, nominal_v: float) -> float:
        """Last good voltage reading (held through dropouts), in pu."""
        v = self.last_good_v.get(self.sensor_at.get((placement, phase)))
        return v / nominal_v if v else 1.0
