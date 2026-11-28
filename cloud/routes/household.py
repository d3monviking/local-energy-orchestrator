"""cloud/routes/household.py — the household (citizen) app. Owner: Household.

Consent, DR eligibility, community statistics and the household summary.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from cloud import db

router = APIRouter()

@router.get("/api/consent/{household_id}")
async def get_consent(household_id: str) -> list[dict]:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT purpose, granted, changed_at FROM consent WHERE household_id = $1 ORDER BY purpose",
            household_id,
        )
    return [{"purpose": r["purpose"], "granted": r["granted"], "changed_at": r["changed_at"].isoformat()} for r in rows]


@router.post("/api/consent/{household_id}/{purpose}")
async def set_consent(household_id: str, purpose: str, granted: bool) -> dict:
    """The citizen app's consent screen (§8.4). Revoking `dr_offers` here
    is what /api/dr_eligibility checks against — the value is the
    consequence (dropping out of the next event), not the toggle itself.
    """
    async with db.pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO consent (household_id, purpose, granted, changed_at) VALUES ($1,$2,$3,now())
               ON CONFLICT (household_id, purpose) DO UPDATE SET granted = $3, changed_at = now()""",
            household_id, purpose, granted,
        )
    return {"household_id": household_id, "purpose": purpose, "granted": granted}


@router.get("/api/dr_eligibility/{household_id}")
async def dr_eligibility_check(
    household_id: str, phase: str = "R", window_start_hour: int = 19, window_end_hour: int = 21,
    # Defaults to a few days past the recorded day's event, past the
    # 3-day cooldown — checking the SAME day as an event everyone just
    # got offered would show "ineligible" before any consent change at
    # all (confirmed directly), which is a confusing baseline for the
    # demo's "revoke -> excluded" beat.
    event_date: str = "2026-05-01",
) -> dict:
    """Live-checks cloud/dr_engine/selection.eligible_households() (the
    real eligibility function, not a re-implementation) against current
    consent — the demonstrable consequence of revoking `dr_offers`
    (§11.3's fairness rule's sibling: consent is a harder gate, checked
    first). Runs the real (sync, psycopg2-based) selection logic in a
    worker thread since this service's pool is asyncpg.
    """
    import psycopg2
    from datetime import date as date_cls
    from cloud.dr_engine.selection import eligible_households

    def _check() -> bool:
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
            dbname=os.environ.get("POSTGRES_DB", "leo"),
        )
        try:
            eligible_pool = eligible_households(conn, phase, date_cls.fromisoformat(event_date), window_start_hour, window_end_hour, run_id="normal")
            return any(hh.household_id == household_id for hh in eligible_pool)
        finally:
            conn.close()

    import asyncio
    eligible = await asyncio.to_thread(_check)
    return {"household_id": household_id, "eligible_for_next_event": eligible}


@router.get("/api/community/{run_id}")
async def community_stats(run_id: str) -> dict:
    """The citizen app's community page (§8.4): battery status, outage
    minutes avoided, total payouts to the neighbourhood."""
    async with db.pool().acquire() as conn:
        total_payout_paise = await conn.fetchval("SELECT coalesce(sum(amount_paise),0) FROM ledger WHERE run_id = $1", run_id)
        n_paid = await conn.fetchval("SELECT count(DISTINCT household_id) FROM ledger WHERE run_id = $1", run_id)
        battery_soc = await conn.fetch(
            "SELECT block_id, avg(soc_after) AS avg_soc, max(soc_after) AS max_soc FROM dispatch WHERE run_id = $1 GROUP BY block_id",
            run_id,
        )
        outage_minutes = await conn.fetchval(
            """SELECT extract(epoch FROM (max(ts) - min(ts))) / 60.0 FROM event
               WHERE run_id = 'outage' AND kind IN ('grid_loss','grid_return')"""
        )
        n_backup_served = await conn.fetchval("SELECT count(DISTINCT household_id) FROM premise_meter WHERE run_id = 'outage'")
    return {
        "run_id": run_id,
        "total_payout_paise": total_payout_paise,
        "n_households_paid": n_paid,
        "battery_status": [dict(r) for r in battery_soc],
        "outage_minutes_protected": float(outage_minutes) if outage_minutes else 0.0,
        "n_backup_served": n_backup_served,
    }


@router.get("/api/citizen/{household_id}")
async def citizen_summary(household_id: str) -> dict:
    """Bundles everything the citizen app's screens need (§8.4) in one
    call: today's offer, earnings by stream, day-late usage, outage/
    backup status, consent — same underlying rows the operator and
    DISCOM surfaces read, just household-scoped.
    """
    async with db.pool().acquire() as conn:
        household = await conn.fetchrow(
            "SELECT id, phase, is_critical, critical_class FROM household WHERE id = $1", household_id
        )
        if household is None:
            raise HTTPException(404, f"no household {household_id!r}")

        consent = await conn.fetch("SELECT purpose, granted FROM consent WHERE household_id = $1", household_id)
        offers = await conn.fetch(
            """SELECT o.event_id, o.level, o.predicted_kwh, o.sent_at, o.replied, o.is_holdout, o.verified_kwh,
                      e.window_start, e.window_end, e.phase
               FROM dr_offer o JOIN dr_event e ON e.run_id = o.run_id AND e.id = o.event_id
               WHERE o.household_id = $1 ORDER BY o.sent_at DESC""",
            household_id,
        )
        ledger_rows = await conn.fetch(
            "SELECT date, entry_type, amount_paise, matched_kwh FROM ledger WHERE household_id = $1 ORDER BY date DESC",
            household_id,
        )
        usage = await conn.fetch(
            """SELECT ts_end, import_kwh, export_kwh, received_at FROM meter_interval
               WHERE household_id = $1 ORDER BY ts_end DESC LIMIT 20""",
            household_id,
        )
        backup = await conn.fetchrow(
            "SELECT priority_class, max_current_a FROM premise_backup WHERE household_id = $1", household_id
        )
        backup_usage = await conn.fetch(
            """SELECT ts_end, backup_kwh, current_a FROM premise_meter
               WHERE run_id = 'outage' AND household_id = $1 ORDER BY ts_end""",
            household_id,
        )

    return {
        "household_id": household_id, "phase": household["phase"], "is_critical": household["is_critical"],
        "critical_class": household["critical_class"],
        "consent": {r["purpose"]: r["granted"] for r in consent},
        "offers": [
            dict(o) | {
                "sent_at": o["sent_at"].isoformat() if o["sent_at"] else None,
                "window_start": o["window_start"].isoformat(), "window_end": o["window_end"].isoformat(),
            }
            for o in offers
        ],
        "earnings": [dict(r) | {"date": r["date"].isoformat()} for r in ledger_rows],
        "total_earnings_paise": sum(r["amount_paise"] for r in ledger_rows),
        "usage": [
            dict(r) | {"ts_end": r["ts_end"].isoformat(), "received_at": r["received_at"].isoformat()}
            for r in usage
        ],
        "is_registered_for_backup": backup is not None,
        "backup_priority_class": backup["priority_class"] if backup else None,
        "backup_usage": [dict(r) | {"ts_end": r["ts_end"].isoformat()} for r in backup_usage],
    }
