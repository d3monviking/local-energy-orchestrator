"""sim/clock.py — simulated clock service. Owner B.

Every other service reads time from here, never from the system clock
(Build Spec v1.0 §2.2). This is what lets a year of simulation run in
minutes and what makes replay deterministic.

Endpoints: GET /now, POST /speed, POST /jump, POST /pause.
Side effect: publishes `leo/clock/tick` on MQTT every real-time TICK_PERIOD_S,
carrying the current simulated time, so any service can stay in sync
without polling /now.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from fastapi import FastAPI
from pydantic import BaseModel, Field

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TICK_TOPIC = "leo/clock/tick"
TICK_PERIOD_S = float(os.environ.get("CLOCK_TICK_PERIOD_S", "1.0"))

DEFAULT_START = datetime.fromisoformat(
    os.environ.get("CLOCK_START", "2026-09-25T00:00:00+00:00")
)


class ClockState:
    """speed is sim-seconds advanced per real second. 0 == paused."""

    def __init__(self, start: datetime) -> None:
        self._lock = threading.Lock()
        self._sim_time = start
        self._speed = 1.0
        self._paused = False

    def now(self) -> datetime:
        with self._lock:
            return self._sim_time

    def advance(self, real_elapsed_s: float) -> datetime:
        with self._lock:
            if not self._paused:
                self._sim_time += timedelta(seconds=real_elapsed_s * self._speed)
            return self._sim_time

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = speed

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused

    def jump_to(self, to: datetime) -> None:
        with self._lock:
            self._sim_time = to

    def jump_by(self, delta_seconds: float) -> datetime:
        with self._lock:
            self._sim_time += timedelta(seconds=delta_seconds)
            return self._sim_time

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "sim_time": self._sim_time.isoformat(),
                "speed": self._speed,
                "paused": self._paused,
            }


state = ClockState(DEFAULT_START)
_mqtt_client: mqtt.Client | None = None


def _connect_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="leo-clock")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


async def _tick_loop() -> None:
    global _mqtt_client
    loop = asyncio.get_event_loop()
    last = loop.time()
    while True:
        await asyncio.sleep(TICK_PERIOD_S)
        now_real = loop.time()
        elapsed = now_real - last
        last = now_real
        sim_now = state.advance(elapsed)
        if _mqtt_client is not None:
            payload = sim_now.isoformat()
            _mqtt_client.publish(TICK_TOPIC, payload, qos=0, retain=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mqtt_client
    try:
        _mqtt_client = _connect_mqtt()
    except Exception as exc:  # pragma: no cover - broker may not be up yet in dev
        print(f"[clock] MQTT connect failed ({exc}); ticking without publish")
        _mqtt_client = None
    task = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        task.cancel()
        if _mqtt_client is not None:
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()


app = FastAPI(title="leo-clock", lifespan=lifespan)


class SpeedRequest(BaseModel):
    speed: float = Field(ge=0, description="sim-seconds per real second; 0 == paused")


class JumpRequest(BaseModel):
    to: datetime | None = None
    delta_seconds: float | None = None


class PauseRequest(BaseModel):
    paused: bool


@app.get("/now")
def now() -> dict:
    return state.snapshot()


@app.post("/speed")
def set_speed(req: SpeedRequest) -> dict:
    state.set_speed(req.speed)
    return state.snapshot()


@app.post("/jump")
def jump(req: JumpRequest) -> dict:
    if req.to is not None:
        to = req.to if req.to.tzinfo else req.to.replace(tzinfo=timezone.utc)
        state.jump_to(to)
    elif req.delta_seconds is not None:
        state.jump_by(req.delta_seconds)
    else:
        raise ValueError("jump requires either 'to' or 'delta_seconds'")
    return state.snapshot()


@app.post("/pause")
def pause(req: PauseRequest) -> dict:
    state.set_paused(req.paused)
    return state.snapshot()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
