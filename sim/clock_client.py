"""sim/clock_client.py — small HTTP client other services use to read
simulated time from sim/clock.py, instead of the system clock. Owner B.
"""

from __future__ import annotations

import os
from datetime import datetime

import requests

CLOCK_URL = os.environ.get("CLOCK_URL", "http://localhost:8010")


def sim_now() -> datetime:
    resp = requests.get(f"{CLOCK_URL}/now", timeout=2)
    resp.raise_for_status()
    return datetime.fromisoformat(resp.json()["sim_time"])
