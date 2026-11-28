"""cloud/routes/operator.py — the operator console. Owner: Grid & DISCOM.

Forecast and explanations, the day-ahead plan and its approval, dispatch,
sensor readings and the backup circuit.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Body, HTTPException

from cloud import db, explain

router = APIRouter()

@router.get("/api/forecast/{run_id}")
async def forecast_for_run(run_id: str) -> dict:
    """The saved day-ahead forecast this run's plan was built from."""
    async with db.pool().acquire() as conn:
        return await explain.forecast(conn, run_id)


@router.get("/api/predicted_events/{run_id}")
async def predicted_events_for_run(run_id: str) -> list[dict]:
    """Events the forecast predicted (with reasons) next to what happened."""
    async with db.pool().acquire() as conn:
        return await explain.predicted_events(conn, run_id)


@router.get("/api/action_log/{run_id}")
async def action_log_for_run(run_id: str) -> list[dict]:
    """Every decision/action in time order, each with its reason."""
    async with db.pool().acquire() as conn:
        return await explain.action_log(conn, run_id)


@router.get("/api/plan/{run_id}")
async def plan_for_run(run_id: str) -> dict:
    """The draft/approved battery plan (§8.3's Plan review screen):
    per-phase setpoint_kw by interval, plus the reserve floor and who
    approved it."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT phase, ts_end, setpoint_kw, mode, planner, reserve_kwh, approved_at, approved_by
               FROM plan WHERE run_id = $1 ORDER BY phase, ts_end""",
            run_id,
        )
        await _ensure_decision_table(conn)
        decisions = await conn.fetch(
            "SELECT decision, decided_by, reason, decided_at FROM plan_decision WHERE run_id = $1 ORDER BY id", run_id)
    by_phase: dict[str, dict] = {}
    for r in rows:
        p = by_phase.setdefault(r["phase"], {
            "phase": r["phase"], "planner": r["planner"], "reserve_kwh": r["reserve_kwh"],
            "approved_at": r["approved_at"].isoformat() if r["approved_at"] else None,
            "approved_by": r["approved_by"], "intervals": [],
        })
        p["intervals"].append({
            "ts_end": r["ts_end"].isoformat(), "setpoint_kw": r["setpoint_kw"], "mode": r["mode"],
        })
    approved = bool(rows) and rows[0]["approved_at"] is not None
    last = decisions[-1]["decision"] if decisions else None
    status = "approved" if approved else "rejected" if last == "rejected" else "pending"
    history = [{"decision": d["decision"], "by": d["decided_by"], "reason": d["reason"],
                "at": d["decided_at"].isoformat()} for d in decisions]
    return {"run_id": run_id, "status": status, "decisions": history, "phases": list(by_phase.values())}


async def _ensure_decision_table(conn) -> None:
    await conn.execute("""CREATE TABLE IF NOT EXISTS plan_decision (
        id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES run(run_id),
        decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'withdrawn')),
        decided_by TEXT NOT NULL, reason TEXT, decided_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


async def _record_decision(conn, run_id: str, decision: str, by: str, reason: str | None = None) -> None:
    await _ensure_decision_table(conn)
    await conn.execute("INSERT INTO plan_decision (run_id, decision, decided_by, reason) VALUES ($1, $2, $3, $4)",
                       run_id, decision, by, reason)


@router.post("/api/plan/{run_id}/{phase}/approve")
async def approve_plan_endpoint(run_id: str, phase: str, approved_by: str = "operator-1") -> dict:
    """The Approve button (§8.3): "the single most important interaction
    in the demo... a human is accountable for what the operator's
    equipment does." Idempotent — intervals already approved (every
    plan sim/loop.py writes is pre-approved, matching the locked
    pre-recorded-video decision) are left as they were.
    """
    async with db.pool().acquire() as conn:
        await conn.execute(
            """UPDATE plan SET approved_at = now(), approved_by = $3
               WHERE run_id = $1 AND phase = $2 AND approved_at IS NULL""",
            run_id, phase, approved_by,
        )
        row = await conn.fetchrow(
            "SELECT approved_at, approved_by FROM plan WHERE run_id = $1 AND phase = $2 LIMIT 1",
            run_id, phase,
        )
    if row is None:
        raise HTTPException(404, f"no plan for run_id={run_id!r} phase={phase!r}")
    return {"run_id": run_id, "phase": phase, "approved_at": row["approved_at"].isoformat(),
            "approved_by": row["approved_by"]}


@router.post("/api/plan/{run_id}/approve")
async def approve_whole_plan(run_id: str, approved_by: str = "operator-1") -> dict:
    """Approve all three phases in one decision (the Plan review screen's
    single Approve). The approval is stamped at the run's own 23:30 IST
    slot on D-1, not wall-clock now(), so replaying a recorded day keeps
    "plan approved" and "DR offers sent" in the right place on the timeline."""
    async with db.pool().acquire() as conn:
        sim_start = await conn.fetchval("SELECT sim_start FROM run WHERE run_id = $1", run_id)
        if sim_start is None:
            raise HTTPException(404, f"no run {run_id!r}")
        stamp = sim_start - timedelta(hours=6)
        await conn.execute(
            "UPDATE plan SET approved_at = $2, approved_by = $3 WHERE run_id = $1 AND approved_at IS NULL",
            run_id, stamp, approved_by,
        )
        await _record_decision(conn, run_id, "approved", approved_by)
        row = await conn.fetchrow(
            "SELECT min(approved_at) AS approved_at, max(approved_by) AS approved_by FROM plan WHERE run_id = $1", run_id)
    return {"run_id": run_id, "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
            "approved_by": row["approved_by"]}


@router.post("/api/plan/{run_id}/withdraw")
async def withdraw_plan(run_id: str, by: str = "operator-1") -> dict:
    """Withdraw approval (undo, or reset the demo). Nothing reaches the
    batteries from an unapproved plan."""
    async with db.pool().acquire() as conn:
        await conn.execute("UPDATE plan SET approved_at = NULL, approved_by = NULL WHERE run_id = $1", run_id)
        await _record_decision(conn, run_id, "withdrawn", by)
    return {"run_id": run_id, "status": "pending"}


@router.post("/api/plan/{run_id}/reject")
async def reject_plan(run_id: str, reason: str = Body(..., embed=True), by: str = "operator-1") -> dict:
    """Reject the plan, with the operator's reason. The batteries get no
    schedule from it and no DR messages go out; the reason is kept with
    the decision so the plan can be revisited."""
    reason = reason.strip()
    if not reason:
        raise HTTPException(422, "a reason is required to reject a plan")
    async with db.pool().acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM plan WHERE run_id = $1 LIMIT 1", run_id) is None:
            raise HTTPException(404, f"no plan for run {run_id!r}")
        await conn.execute("UPDATE plan SET approved_at = NULL, approved_by = NULL WHERE run_id = $1", run_id)
        await _record_decision(conn, run_id, "rejected", by, reason)
    return {"run_id": run_id, "status": "rejected", "reason": reason}


@router.get("/api/dispatch/{run_id}")
async def dispatch_for_run(run_id: str) -> list[dict]:
    """Live operations view (§8.3): SoC across the three blocks, mode,
    live-rule triggers, over the day."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered
               FROM dispatch WHERE run_id = $1 ORDER BY block_id, ts_end""",
            run_id,
        )
    return [dict(r) | {"ts_end": r["ts_end"].isoformat()} for r in rows]


@router.get("/api/sensor_readings/{run_id}")
async def sensor_readings_for_run(run_id: str) -> list[dict]:
    """Per-phase voltage traces (§8.3's live operations view)."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT sensor_id, ts_end, voltage_v, supply_present FROM sensor_reading
               WHERE run_id = $1 ORDER BY sensor_id, ts_end""",
            run_id,
        )
    return [dict(r) | {"ts_end": r["ts_end"].isoformat()} for r in rows]


@router.get("/api/backup_households/{run_id}")
async def backup_households_for_run(run_id: str) -> list[dict]:
    """Households actually served backup power during this run — the
    map's "households on the backup circuit are visually distinct"
    requirement (§8.2), for the outage run's event replay. Returns
    bus_id alongside household_id since that's what the map indexes by.
    """
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT pm.household_id, h.bus_id FROM premise_meter pm
               JOIN household h ON h.id = pm.household_id WHERE pm.run_id = $1""",
            run_id,
        )
    return [dict(r) for r in rows]


# Same classes and limits as gateway/outage.py (the allocator that ran in the sim).
BACKUP_PRIORITY = {"health": 1, "water": 2, "livelihood": 3, "education": 3}
BACKUP_LIMIT_A = {1: 16.0, 2: 10.0, 3: 6.0}


@router.get("/api/backup_premises/{run_id}")
async def backup_premises(run_id: str) -> list[dict]:
    """Critical premises on the backup circuit, in priority order, with the
    backup power each actually drew per interval — the console's backup panel."""
    async with db.pool().acquire() as conn:
        nominal = await conn.fetchval("SELECT nominal_v_ln FROM neighbourhood LIMIT 1") or 250.0
        prem = await conn.fetch("SELECT id, phase::text AS phase, critical_class FROM household WHERE is_critical")
        rows = await conn.fetch(
            "SELECT household_id, ts_end, backup_kwh, current_a, limit_active FROM premise_meter WHERE run_id = $1 ORDER BY ts_end",
            run_id,
        )
    by_id: dict[str, list] = {}
    for r in rows:
        by_id.setdefault(r["household_id"], []).append({
            "ts_end": r["ts_end"].isoformat(), "kw": round(r["backup_kwh"] * 4, 2),
            "current_a": r["current_a"], "limit_active": r["limit_active"],
        })
    out = []
    for p in prem:
        pr = BACKUP_PRIORITY.get(p["critical_class"])
        if pr is None:
            continue
        out.append({"household_id": p["id"], "phase": p["phase"], "critical_class": p["critical_class"],
                    "priority": pr, "limit_a": BACKUP_LIMIT_A[pr], "limit_kw": BACKUP_LIMIT_A[pr] * nominal / 1000,
                    "intervals": by_id.get(p["id"], [])})
    return sorted(out, key=lambda x: (x["priority"], x["household_id"]))
