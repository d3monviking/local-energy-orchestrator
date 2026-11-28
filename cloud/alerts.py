"""cloud/alerts.py — turn what happened (the `event` table) into household alerts.

The gateway and the simulator only record events; this job decides who
hears about each one and sends it through cloud/messaging.notify
(contracts/events.md, C-1 and C-4). Each event is sent once per household
and channel (dedupe key "event-<id>"), so the job can be run any number of
times, after a recording or continuously:

    POSTGRES_PORT=5433 python -m cloud.alerts --run-id load_shedding
    POSTGRES_PORT=5433 python -m cloud.alerts --all
    POSTGRES_PORT=5433 python -m cloud.alerts --all --follow     # poll for new events

It also makes sure every DR offer recorded as sent has its message, which
covers runs whose offers were copied from another run.

When each alert goes out (simulated time):
    most events          at the event
    dr_auto_ac           3 h before the event window, ahead of the 2 h pre-cool,
                         so households can opt out first
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import timedelta

import psycopg2

from cloud.dr_engine.selection import offer_dedupe_key, send_offers
from cloud.messaging import notify, resolve_audience

ALERTABLE = ("load_shedding_notice", "grid_loss", "backup_start", "restore", "grid_return",
             "overload_trip", "dr_auto_shift", "dr_auto_ac")
AC_NOTICE = timedelta(hours=3)
AC_DEFAULT_DURATION = timedelta(hours=2)
RESTORE_WINDOW = timedelta(hours=1)   # restore batches follow grid return within minutes
OFFER_NOTICE = timedelta(hours=20)   # sim/loop.py runs the DR event with hours_notice=20


def connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def _scope_audience(conn, scope: str | None):
    """'upstream' / 'all' -> everyone; 'phase:X' -> that phase; 'local:<bus>' -> that bus."""
    if scope in (None, "upstream", "all"):
        return "all"
    if scope.startswith("phase:"):
        return scope
    if scope.startswith("local:"):
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM household WHERE bus_id = %s ORDER BY id", (scope[6:],))
            return [r[0] for r in cur.fetchall()]
    raise ValueError(f"unknown event scope {scope!r}")


def _nominal_v(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT nominal_v_ln FROM neighbourhood LIMIT 1")
        row = cur.fetchone()
    return float(row[0]) if row else 230.0


def _premises(conn, run_id: str, kind: str, start, end) -> set[str]:
    """Household ids named by `kind` events (backup_start / restore) in [start, end]."""
    with conn.cursor() as cur:
        cur.execute("""SELECT payload FROM event WHERE run_id = %s AND kind::text = %s AND ts BETWEEN %s AND %s""",
                    (run_id, kind, start, end))
        ids: set[str] = set()
        for (p,) in cur.fetchall():
            ids |= {pr["household_id"] for pr in (p or {}).get("premises", [])}
            ids |= set((p or {}).get("household_ids", []))
    return ids


def _without(conn, audience, exclude: set[str]):
    if not exclude:
        return audience
    return [h for h, _ in resolve_audience(conn, audience) if h not in exclude]


def plan_alert(conn, run_id: str, run_start, ev: dict):
    """(audience, kind, payload, at) for one event, or None if households
    aren't told about it."""
    kind, p, ts = ev["kind"], ev["payload"] or {}, ev["ts"]
    if kind == "load_shedding_notice":
        return "all", kind, {"start": p["scheduled_start"], "end": p["scheduled_end"]}, ts
    if kind == "grid_loss":
        # Premises LEO keeps on backup hear "you're on backup" (backup_start) instead.
        audience = _without(conn, _scope_audience(conn, ev["scope"]), _premises(conn, run_id, "backup_start", ts, ts))
        return (audience, kind,
                {"expected_end": p.get("expected_end"), "planned": p.get("cause") == "load_shedding"}, ts) if audience else None
    if kind == "backup_start":
        v = _nominal_v(conn)
        per = {pr["household_id"]: {"limit_kw": pr.get("allocated_kw", 0.0),
                                    "limit_a": pr.get("allocated_kw", 0.0) * 1000.0 / v}
               for pr in p.get("premises", [])}
        return (list(per), kind, {"per_household": per}, ts) if per else None
    if kind == "restore":
        ids = p.get("household_ids") or []
        return (ids, kind, {}, ts) if ids else None
    if kind == "grid_return":
        # Backup premises are still islanded until their restore batch, and
        # hear "back on grid" (restore) then, not "power is back" now.
        audience = _without(conn, _scope_audience(conn, ev["scope"]),
                            _premises(conn, run_id, "restore", ts, ts + RESTORE_WINDOW))
        return (audience, kind, {}, ts) if audience else None
    if kind == "overload_trip":
        return _scope_audience(conn, ev["scope"]), kind, {}, ts
    if kind == "dr_auto_shift":
        homes = p.get("pump_homes") or []
        return (homes, kind, {"slot_ist": p["pump_slot_ist"], "day": run_start}, ts) if homes else None
    if kind == "dr_auto_ac":
        homes = p.get("ac_homes") or []
        if not homes:
            return None
        with conn.cursor() as cur:   # the event's window, if a DR event was recorded for it
            cur.execute("SELECT window_end FROM dr_event WHERE run_id = %s AND window_start = %s LIMIT 1",
                        (run_id, ts))
            row = cur.fetchone()
        end = row[0] if row else ts + AC_DEFAULT_DURATION
        return homes, kind, {"start": ts, "end": end, "payment_rs": p.get("ac_payment_rs", 0)}, ts - AC_NOTICE
    return None


def dispatch_alerts(conn, run_id: str) -> int:
    """Send every not-yet-sent alert for `run_id`. Returns messages created."""
    with conn.cursor() as cur:
        cur.execute("SELECT sim_start FROM run WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"no run {run_id!r}")
        run_start = row[0]
        cur.execute("""SELECT id, ts, kind::text, scope, payload FROM event
                       WHERE run_id = %s AND kind::text = ANY(%s) ORDER BY ts, id""", (run_id, list(ALERTABLE)))
        events = [dict(zip(("id", "ts", "kind", "scope", "payload"), r)) for r in cur.fetchall()]
    created = 0
    for ev in events:
        planned = plan_alert(conn, run_id, run_start, ev)
        if planned is None:
            continue
        audience, kind, payload, at = planned
        created += len(notify(conn, run_id, audience, kind, payload, at=at, dedupe_key=f"event-{ev['id']}"))
    created += sync_offers(conn, run_id)
    return created


def sync_offers(conn, run_id: str) -> int:
    """Every dr_offer recorded as sent gets its message (offers copied into
    a run by sim/loop.py arrive without one). Returns messages created."""
    with conn.cursor() as cur:
        cur.execute("""SELECT o.event_id, o.household_id, o.level, o.sent_at, e.window_start, e.window_end
                       FROM dr_offer o JOIN dr_event e ON e.run_id = o.run_id AND e.id = o.event_id
                       WHERE o.run_id = %s AND o.sent_at IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM message m WHERE m.run_id = o.run_id
                                         AND m.household_id = o.household_id
                                         AND m.dedupe_key = 'offer-' || o.event_id)""", (run_id,))
        rows = cur.fetchall()
    created = 0
    by_event: dict[tuple, dict] = {}
    for event_id, hh, level, sent_at, ws, we in rows:
        if sent_at >= ws:
            # Recorded before B1b, when the SMS mock stamped its own wall
            # clock: an offer "sent" after its event began. Use the notice
            # the recording asked for, and fix the record to match.
            sent_at = ws - OFFER_NOTICE
            with conn.cursor() as cur:
                cur.execute("UPDATE dr_offer SET sent_at = %s WHERE run_id = %s AND event_id = %s AND household_id = %s",
                            (sent_at, run_id, event_id, hh))
            conn.commit()
        by_event.setdefault((event_id, sent_at, ws, we), {})[hh] = level
    for (event_id, sent_at, ws, we), levels in by_event.items():
        before = _count(conn, run_id, offer_dedupe_key(event_id))
        send_offers(conn, run_id, event_id, levels, ws, we, sent_at)
        created += _count(conn, run_id, offer_dedupe_key(event_id)) - before
    return created


def _count(conn, run_id: str, dedupe_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM message WHERE run_id = %s AND dedupe_key = %s", (run_id, dedupe_key))
        return cur.fetchone()[0]


def _all_runs(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM run ORDER BY sim_start, run_id")
        return [r[0] for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--run-id", action="append", help="run(s) to process")
    who.add_argument("--all", action="store_true", help="every recorded run")
    ap.add_argument("--follow", action="store_true", help="keep polling for new events")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between polls with --follow")
    args = ap.parse_args()
    conn = connect()
    while True:
        for run_id in (_all_runs(conn) if args.all else args.run_id):
            try:
                n = dispatch_alerts(conn, run_id)
            except (psycopg2.Error, ValueError) as exc:
                # A run being (re-)recorded can vanish mid-pass or not exist yet.
                conn.rollback()
                if not args.follow:
                    raise
                print(f"{run_id}: skipped this pass ({type(exc).__name__}: {str(exc).splitlines()[0]})", flush=True)
                continue
            if n or not args.follow:
                print(f"{run_id}: {n} message(s) sent", flush=True)
        if not args.follow:
            break
        time.sleep(args.interval)
    conn.close()


if __name__ == "__main__":
    main()
