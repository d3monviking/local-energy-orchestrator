"""sim/measure.py — truth to observable. Owner B. Build Specification
v1.0 §7.3.

LEO is never handed the truth sim/loop.py works with (true per-interval
household load/PV, exact bus voltage) — only what real infrastructure
would actually deliver: noisy sensor readings with occasional dropouts,
and meter intervals that arrive a day late on the AMI SLA schedule.
These functions are that boundary. Battery telemetry isn't synthesized
here: the mock Modbus server (mocks/modbus_battery.py) is itself the
authoritative source once sim/loop.py writes a setpoint to it, exactly
as a real inverter would be.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone

import numpy as np

from mocks.ies_server import release_delay_hours

SENSOR_VOLTAGE_NOISE_V = 0.3
SENSOR_DROPOUT_PROB = 0.02  # "occasional dropouts", Build Spec §7.3

METER_IMPORT_NOISE_FRAC = 0.01  # meter-class accuracy, not sensor-class
METER_MISSING_INTERVAL_PROB = 0.01  # "some intervals missing", §7.3


def sensor_reading(
    true_voltage_v: float, supply_present: bool, rng: np.random.Generator
) -> dict | None:
    """One sensor's reading for one interval. Returns None on a dropout —
    the caller simply doesn't get a row for that (sensor, interval), the
    same as a real LoRa packet that never arrives.
    """
    if rng.random() < SENSOR_DROPOUT_PROB:
        return None
    return {
        "voltage_v": float(true_voltage_v + rng.normal(0, SENSOR_VOLTAGE_NOISE_V)),
        "supply_present": bool(supply_present),
        "provenance": "simulated",
    }


def meter_interval(
    household_id: str,
    ts_end: datetime,
    true_import_kwh: float,
    true_export_kwh: float,
    true_voltage_v: float,
    rng: np.random.Generator,
) -> dict | None:
    """One household's one 15-minute interval, as it will eventually
    reach LEO: metered (slightly noisy) values, plus `received_at` drawn
    from the same AMISP SLA distribution mocks/ies_server.py uses to
    release daily files — the two must agree, or a recorded run's
    `meter_interval.received_at` would promise data earlier than the mock
    IES endpoint would actually have released it.

    Returns None for the `METER_MISSING_INTERVAL_PROB` share of intervals
    that never arrive at all (§7.3).
    """
    if rng.random() < METER_MISSING_INTERVAL_PROB:
        return None

    import_kwh = max(0.0, true_import_kwh * (1 + rng.normal(0, METER_IMPORT_NOISE_FRAC)))
    export_kwh = max(0.0, true_export_kwh * (1 + rng.normal(0, METER_IMPORT_NOISE_FRAC)))
    avg_voltage_v = true_voltage_v + rng.normal(0, SENSOR_VOLTAGE_NOISE_V)

    day = ts_end.date() if ts_end.hour != 0 or ts_end.minute != 0 else (ts_end - timedelta(minutes=1)).date()
    delay_h = release_delay_hours(household_id, day)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    received_at = day_end + timedelta(hours=delay_h)

    return {
        "household_id": household_id,
        "ts_end": ts_end,
        "import_kwh": round(import_kwh, 4),
        "export_kwh": round(export_kwh, 4),
        "avg_voltage_v": round(avg_voltage_v, 2),
        "received_at": received_at,
        "provenance": "simulated",
    }


if __name__ == "__main__":
    rng = np.random.default_rng(3)

    n_dropouts = sum(1 for _ in range(2000) if sensor_reading(230.0, True, rng) is None)
    print(f"sensor dropout rate over 2000 draws: {n_dropouts/2000:.3f} (target {SENSOR_DROPOUT_PROB})")

    reading = sensor_reading(230.0, True, rng)
    print(f"sample sensor reading: {reading}")

    ts_end = datetime(2026, 4, 27, 19, 45, tzinfo=timezone.utc)
    row = meter_interval("HH-0031", ts_end, true_import_kwh=1.2, true_export_kwh=0.0, true_voltage_v=228.0, rng=rng)
    print(f"sample meter interval: {row}")
    delay_h = (row["received_at"] - datetime.combine(ts_end.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)).total_seconds() / 3600
    print(f"release delay for this household/day: {delay_h:.2f}h after day-end (matches ies_server's schedule)")
