"""mocks/sms_service.py — FastAPI mock messaging service. Owner B.

Real interface preserved: a REST send endpoint, a delivery log, and
inbound replies. In deployment this becomes an SMS/IVR gateway; nothing
upstream (DR engine, outage alerts) needs to change to swap it in.

sim/personas.py (Day 3) posts replies here on behalf of simulated
households; the citizen app and operator console both read /messages.
"""

from __future__ import annotations

import itertools
import os
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from sim.clock_client import sim_now
except ImportError:  # pragma: no cover - allows running standalone
    def sim_now() -> datetime:
        return datetime.utcnow()

app = FastAPI(title="leo-mock-sms")

_id_counter = itertools.count(1)
_messages: dict[int, dict] = {}


class SendRequest(BaseModel):
    to: str  # household_id
    channel: Literal["sms", "ivr", "email"] = "sms"
    body: str
    meta: dict = {}


class ReplyRequest(BaseModel):
    body: str


@app.post("/send")
def send(req: SendRequest) -> dict:
    msg_id = next(_id_counter)
    record = {
        "id": msg_id,
        "to": req.to,
        "channel": req.channel,
        "body": req.body,
        "meta": req.meta,
        "sent_at": sim_now().isoformat(),
        "delivered": True,
        "reply": None,
        "replied_at": None,
    }
    _messages[msg_id] = record
    return record


@app.get("/messages")
def list_messages(to: str | None = None) -> list[dict]:
    values = list(_messages.values())
    if to is not None:
        values = [m for m in values if m["to"] == to]
    return values


@app.get("/messages/{msg_id}")
def get_message(msg_id: int) -> dict:
    if msg_id not in _messages:
        raise HTTPException(404, "message not found")
    return _messages[msg_id]


@app.post("/messages/{msg_id}/reply")
def reply(msg_id: int, req: ReplyRequest) -> dict:
    if msg_id not in _messages:
        raise HTTPException(404, "message not found")
    record = _messages[msg_id]
    record["reply"] = req.body
    record["replied_at"] = sim_now().isoformat()
    return record


@app.post("/_reset")
def reset() -> dict:
    """Dev-only: clear the log between recorded runs."""
    _messages.clear()
    return {"status": "cleared"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
