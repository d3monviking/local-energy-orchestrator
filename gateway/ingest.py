"""gateway/ingest.py — MQTT + Modbus -> Postgres, validation, provenance.
Owner B. Build Spec v1.0 §4.3 (C4).

Day 1 scope: boot the service so `docker compose up` works end to end.
The real ingestion path (MQTT subscriber on leo/sensor/+/..., Modbus
polling of the three battery blocks, de-dup on (device, register,
interval end), provenance tagging per Architecture v3.0 §8 C4) lands
once sim/loop + sim/measure (Day 3) are producing something to ingest.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="leo-gateway")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gateway"}
