"""mocks/ies_server.py — FastAPI mock of the India Energy Stack / DISCOM
MDMS export, releasing each day's household meter file on the real AMISP
SLA schedule (95% within 8h, 98% within 12h, 99.5% within 24h — Build
Spec v1.0 §9.1 / Architecture v3.0 §3.1). Owner B.

Real interface preserved: cloud/connectors.py pulls from here exactly as
it would pull from a real IES adapter or DISCOM export; only the
transport (REST here vs Beckn/SFTP/Kafka in deployment) changes.

Per-household release delay is deterministic (seeded from household_id +
date) so repeated requests and server restarts agree on what has
"arrived" at any given simulated time, without needing persistence.

Data source: until sim/loop + sim/measure (Day 3) produce real masked
meter_interval rows, SyntheticMeterSource fabricates plausible per-15-min
import/export so the SLA-release mechanism and connector/ingestion path
are testable now. Swapping to the real truth-to-observable pipeline later
means swapping the source, not this endpoint's shape.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Protocol

import psycopg2
from fastapi import FastAPI, HTTPException

try:
    from sim.clock_client import sim_now
except ImportError:  # pragma: no cover
    def sim_now() -> datetime:
        return datetime.now(timezone.utc)

app = FastAPI(title="leo-mock-ies")

INTERVAL_MIN = 15
INTERVALS_PER_DAY = 24 * 60 // INTERVAL_MIN


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"),
        password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def _household_ids() -> list[str]:
    try:
        conn = _db_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM household ORDER BY id")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def release_delay_hours(household_id: str, day: date_cls) -> float:
    """Deterministic draw from the contractual SLA distribution."""
    key = f"{household_id}:{day.isoformat()}".encode()
    digest = hashlib.sha256(key).digest()
    u = int.from_bytes(digest[:4], "big") / 2**32  # uniform in [0,1)
    frac = int.from_bytes(digest[4:8], "big") / 2**32  # independent uniform within the bucket

    if u < 0.95:
        lo, hi = 0.0, 8.0
    elif u < 0.98:
        lo, hi = 8.0, 12.0
    elif u < 0.995:
        lo, hi = 12.0, 24.0
    else:
        lo, hi = 24.0, 48.0
    return lo + frac * (hi - lo)


def release_time(household_id: str, day: date_cls) -> datetime:
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return day_end + timedelta(hours=release_delay_hours(household_id, day))


class MeterDataSource(Protocol):
    def intervals_for(self, household_id: str, day: date_cls) -> list[dict]: ...


class SyntheticMeterSource:
    """Deterministic placeholder, not the real world-builder truth.
    Replace with a reader over sim/measure's masked output once Day 3 lands.
    """

    def intervals_for(self, household_id: str, day: date_cls) -> list[dict]:
        seed = int(hashlib.sha256(f"{household_id}:{day.isoformat()}".encode()).hexdigest()[:8], 16)
        rows = []
        for i in range(INTERVALS_PER_DAY):
            ts_end = datetime.combine(day, time.min, tzinfo=timezone.utc) + timedelta(minutes=INTERVAL_MIN * (i + 1))
            hour = ts_end.hour
            base_kw = 0.3 + 0.9 * max(0.0, 1.0 - abs(hour - 20) / 6.0)  # evening-peaked shape
            jitter = ((seed >> (i % 24)) % 1000) / 1000.0 * 0.2
            import_kwh = round((base_kw + jitter) * (INTERVAL_MIN / 60.0), 4)
            rows.append({
                "ts_end": ts_end.isoformat(),
                "import_kwh": import_kwh,
                "export_kwh": 0.0,
                "avg_voltage_v": 235.0,
            })
        return rows


_source: MeterDataSource = SyntheticMeterSource()


@app.get("/files/{day}")
def get_file(day: date_cls) -> dict:
    now = sim_now()
    households = _household_ids()
    if not households:
        raise HTTPException(503, "household registry empty; run world.build first")

    released, pending = [], 0
    for hh in households:
        rel_time = release_time(hh, day)
        if now >= rel_time:
            released.append({
                "household_id": hh,
                "received_at": rel_time.isoformat(),
                "intervals": _source.intervals_for(hh, day),
            })
        else:
            pending += 1

    return {
        "day": day.isoformat(),
        "sim_time": now.isoformat(),
        "households_released": len(released),
        "households_pending": pending,
        "households": released,
    }


@app.get("/files/{day}/status")
def file_status(day: date_cls) -> dict:
    now = sim_now()
    households = _household_ids()
    released = sum(1 for hh in households if now >= release_time(hh, day))
    return {"day": day.isoformat(), "sim_time": now.isoformat(),
            "total": len(households), "released": released}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
