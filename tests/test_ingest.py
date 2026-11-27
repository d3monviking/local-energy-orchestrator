"""gateway/ingest.py: C4 validation and the readings C7 gets to see."""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from gateway.ingest import IngestError, Ingestor

SENSORS = pd.DataFrame([
    {"id": "SEN-BUSBAR-R", "bus_id": "B0", "phase": "R", "placement": "busbar", "dev_eui": "aaaa"},
    {"id": "SEN-FAREND-R", "bus_id": "B9", "phase": "R", "placement": "far_end", "dev_eui": "bbbb"},
])
T0 = "2026-04-27T13:00:00+00:00"


def up(eui="aaaa", batch_end=T0, volts=(248.0,), supply=True, kw=41.5, topic_eui=None):
    obj = {"interval_s": 900, "batch_end": batch_end, "voltages_v": list(volts), "supply_present": supply}
    if kw is not None:
        obj["active_power_kw"] = kw
    return f"leo/sensor/{topic_eui or eui}/up", json.dumps({"devEUI": eui, "fCnt": 1, "object": obj}).encode()


def test_valid_busbar_uplink_feeds_ct_and_voltage():
    ing = Ingestor(SENSORS)
    u = ing.uplink(*up())
    assert u.sensor_id == "SEN-BUSBAR-R" and u.active_power_kw == 41.5
    at = datetime.fromisoformat(T0)
    assert ing.phase_load_kw("R", at) == 41.5
    assert ing.voltage_pu("busbar", "R", 250.0) == pytest.approx(248.0 / 250.0)


@pytest.mark.parametrize("msg,reason", [
    (up(eui="ffff"), "unknown_device"),
    (up(topic_eui="bbbb"), "topic_mismatch"),
    (up(volts=(999.0,)), "implausible_value"),
    (up(batch_end="2026-04-27T13:00:00"), "bad_timestamp"),
    (up(eui="bbbb", kw=3.0), "unexpected_field"),
    (("leo/sensor/aaaa/up", b"not json"), "bad_json"),
])
def test_rejections_are_counted_by_reason(msg, reason):
    ing = Ingestor(SENSORS)
    with pytest.raises(IngestError):
        ing.uplink(*msg)
    assert ing.rejected[reason] == 1


def test_duplicate_interval_is_dropped():
    ing = Ingestor(SENSORS)
    assert ing.uplink(*up()) is not None
    assert ing.uplink(*up()) is None
    assert ing.rejected["duplicate_or_stale"] == 1


def test_voltage_is_held_through_an_outage_and_ct_goes_stale():
    ing = Ingestor(SENSORS)
    ing.uplink(*up())
    ing.uplink(*up(batch_end="2026-04-27T13:15:00+00:00", volts=(), supply=False, kw=None))
    assert ing.voltage_pu("busbar", "R", 250.0) == pytest.approx(248.0 / 250.0)
    assert ing.phase_load_kw("R", datetime(2026, 4, 27, 13, 15, tzinfo=timezone.utc)) is None
    assert ing.voltage_pu("far_end", "R", 250.0) == 1.0  # never heard from: nominal


def test_power_event_parsed():
    ing = Ingestor(SENSORS)
    ev = ing.event("leo/sensor/bbbb/event", json.dumps({"devEUI": "bbbb", "object": {
        "event": "power_fail", "ts": "2026-04-27T14:10:00Z", "last_voltage_v": 231.0}}).encode())
    assert ev.kind == "power_fail" and ev.phase == "R" and ev.ts.tzinfo is not None
