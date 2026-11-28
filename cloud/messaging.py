"""cloud/messaging.py — every message LEO sends to households (contracts/events.md, C-4).

Nothing else in LEO writes to the SMS service or the alert feed. Callers
say who should hear what; `notify` resolves the audience, renders each
household's text in its language (cloud/message_templates.py), stores one
`message` row per household per channel, and delivers:

    sms   POST to the SMS service (mocks/sms_service.py today; a DLT-registered
          provider in the field). A failed send is stored as delivered = false
          with the reason, never as sent.
    app   the household's in-app alert feed (the Android app reads it).
    ivr   reserved; not built.

`at` is the *simulated* time of sending. With a `dedupe_key`, the same
message goes to a household on a channel at most once per run, so callers
(the event-to-alert job, re-runs) can retry safely.

`notify` commits the caller's connection: once an SMS has left, the record
of it must not be rolled back.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Iterable, Sequence

import pandas as pd
import psycopg2.extras
import requests

from cloud.message_templates import render

CHANNELS = ("sms", "app", "ivr")
SMS_TIMEOUT_S = 3.0


def default_sms_url() -> str:
    return os.environ.get("SMS_URL", "http://localhost:8012")


def resolve_audience(conn, audience: str | Sequence[str]) -> list[tuple[str, str]]:
    """(household_id, lang) for an audience: 'all', 'phase:R|Y|B',
    'backup_premises', or an explicit list of household ids."""
    with conn.cursor() as cur:
        if isinstance(audience, str):
            if audience == "all":
                cur.execute("SELECT id, lang FROM household ORDER BY id")
            elif audience.startswith("phase:") and audience[6:] in ("R", "Y", "B"):
                cur.execute("SELECT id, lang FROM household WHERE phase = %s ORDER BY id", (audience[6:],))
            elif audience == "backup_premises":
                cur.execute("""SELECT h.id, h.lang FROM household h
                               JOIN premise_backup pb ON pb.household_id = h.id ORDER BY h.id""")
            else:
                raise ValueError(f"unknown audience {audience!r}")
            return [(r[0], r[1]) for r in cur.fetchall()]
        ids = list(audience)
        cur.execute("SELECT id, lang FROM household WHERE id = ANY(%s) ORDER BY id", (ids,))
        rows = [(r[0], r[1]) for r in cur.fetchall()]
    unknown = set(ids) - {r[0] for r in rows}
    if unknown:
        raise ValueError(f"unknown household id(s): {sorted(unknown)}")
    return rows


def _fields_for(payload: dict, household_id: str) -> dict:
    """The payload's shared fields plus this household's own
    (payload['per_household'][household_id]), e.g. a backup limit."""
    shared = {k: v for k, v in payload.items() if k != "per_household"}
    return {**shared, **payload.get("per_household", {}).get(household_id, {})}


def _jsonable(fields: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (datetime, date)) else v) for k, v in fields.items()}


def notify(
    conn,
    run_id: str,
    audience: str | Sequence[str],
    kind: str,
    payload: dict,
    at,
    channels: Iterable[str] = ("sms", "app"),
    dedupe_key: str | None = None,
    sms_url: str | None = None,
) -> list[int]:
    """Send `kind` to `audience` at simulated time `at`. Returns the ids of
    the message rows created (none for households/channels deduped away).
    `sms_url` overrides the SMS_URL environment variable."""
    channels = tuple(channels)
    bad = [c for c in channels if c not in CHANNELS]
    if bad:
        raise ValueError(f"unknown channel(s) {bad}")
    if "ivr" in channels:
        raise NotImplementedError("IVR delivery is not built yet")
    sent_at = pd.Timestamp(at)
    if sent_at.tzinfo is None:
        raise ValueError("`at` must be timezone-aware (simulated UTC time)")
    sent_at = sent_at.to_pydatetime()

    # Render everything before sending anything: a bad template must fail
    # before the first SMS leaves, not halfway through the neighbourhood.
    outbox = []
    for household_id, lang in resolve_audience(conn, audience):
        fields = _fields_for(payload, household_id)
        outbox.append((household_id, lang, render(kind, lang, fields, sent_at), psycopg2.extras.Json(_jsonable(fields))))

    sms_base = sms_url or default_sms_url()
    created: list[int] = []
    sms_down: str | None = None   # once the SMS service is unreachable, don't wait on it per household
    session = requests.Session()
    try:
        with conn.cursor() as cur:
            for household_id, lang, body, stored in outbox:
                for channel in channels:
                    cur.execute(
                        """INSERT INTO message (run_id, household_id, kind, channel, lang, body, payload,
                                                sent_at, delivered, dedupe_key)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id, household_id, channel, dedupe_key)
                               WHERE dedupe_key IS NOT NULL DO NOTHING
                           RETURNING id""",
                        (run_id, household_id, kind, channel, lang, body, stored, sent_at,
                         channel == "app", dedupe_key),
                    )
                    row = cur.fetchone()
                    if row is None:
                        continue  # already sent under this dedupe_key
                    message_id = row[0]
                    created.append(message_id)
                    if channel != "sms":
                        continue
                    if sms_down:
                        cur.execute("UPDATE message SET error = %s WHERE id = %s", (sms_down, message_id))
                        continue
                    try:
                        resp = session.post(f"{sms_base}/send", timeout=SMS_TIMEOUT_S, json={
                            "to": household_id, "channel": "sms", "body": body, "sent_at": sent_at.isoformat(),
                            "meta": {"run_id": run_id, "kind": kind, "message_id": message_id}})
                        resp.raise_for_status()
                        cur.execute("UPDATE message SET delivered = TRUE, provider_id = %s WHERE id = %s",
                                    (str(resp.json().get("id")), message_id))
                    except (requests.ConnectionError, requests.Timeout) as exc:
                        sms_down = f"sms service unreachable: {type(exc).__name__}"
                        cur.execute("UPDATE message SET error = %s WHERE id = %s", (sms_down, message_id))
                    except (requests.RequestException, ValueError) as exc:
                        cur.execute("UPDATE message SET error = %s WHERE id = %s", (f"sms rejected: {exc}", message_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        session.close()
    return created


def record_reply(conn, run_id: str, household_id: str, dedupe_key: str, reply: str, at) -> int:
    """A household's reply (SMS "YES"/"STOP", or a tap in the app) to the
    message sent under `dedupe_key`. Stored on that household's message
    rows; returns how many were updated (0 = no such message). Acting on
    the reply (accepting an offer, an override) is the caller's job.
    Commits."""
    replied_at = pd.Timestamp(at)
    if replied_at.tzinfo is None:
        raise ValueError("`at` must be timezone-aware (simulated UTC time)")
    with conn.cursor() as cur:
        cur.execute("""UPDATE message SET reply = %s, replied_at = %s
                       WHERE run_id = %s AND household_id = %s AND dedupe_key = %s""",
                    (reply.strip(), replied_at.to_pydatetime(), run_id, household_id, dedupe_key))
        n = cur.rowcount
    conn.commit()
    return n
