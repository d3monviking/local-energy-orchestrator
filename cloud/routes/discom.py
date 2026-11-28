"""cloud/routes/discom.py — the DISCOM dashboard. Owner: Grid & DISCOM.

Recommendations and their workflow, impact and economics.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from cloud import db

router = APIRouter()

@router.get("/api/impact")
async def impact(sweep_id: str | None = None) -> dict:
    """Reliability metrics and unit economics from the year-sampled sweep
    (eval/sweep.py + eval/economics.py), plus a per-month breakdown."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT sweep_id, computed_at, result FROM eval_economics
               WHERE ($1::text IS NULL OR sweep_id = $1)
               ORDER BY (sweep_id = 'year') DESC, computed_at DESC LIMIT 1""", sweep_id)
        if row is None:
            raise HTTPException(404, "no sweep has been evaluated yet - run eval.sweep then eval.economics")
        monthly = await conn.fetch(
            """SELECT config, date_trunc('month', day)::date AS month,
                      avg((metrics->>'evening_peak_kw')::float) AS evening_peak_kw,
                      avg((metrics->>'peak_loading_pct')::float) AS peak_loading_pct,
                      sum((metrics->>'aging_hours')::float) AS aging_hours,
                      sum((metrics->>'battery_discharge_kwh')::float) AS discharge_kwh,
                      sum((metrics->'dr'->>'verified_kwh')::float) AS dr_kwh,
                      sum((metrics->>'cust_min_under')::float) / 60 AS undervoltage_cust_h,
                      sum((metrics->>'crit_out_min')::float) / 60 AS crit_out_h,
                      sum((metrics->>'crit_served_min')::float) / 60 AS crit_served_h,
                      max((metrics->>'temp_max_c')::float) AS temp_max_c
               FROM eval_day WHERE sweep_id = $1 GROUP BY 1, 2 ORDER BY 2, 1""", row["sweep_id"])
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    return {"sweep_id": row["sweep_id"], "computed_at": row["computed_at"].isoformat(), **result,
            "monthly": [dict(r) | {"month": r["month"].isoformat()} for r in monthly]}


@router.get("/api/recommendations/{run_id}")
async def recommendations_for_run(run_id: str) -> list[dict]:
    """The DISCOM action queue (§8.5) and the operator's residual-gap
    view both read this — same rows, different surface."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT rec_id, dt_id, phase, window_start, window_end, issue, severity,
                      residual_gap_kw, local_actions, recommended_action, evidence, status, status_changed_at
               FROM recommendation WHERE run_id = $1 ORDER BY window_start""",
            run_id,
        )
    return [
        {
            "rec_id": r["rec_id"], "dt_id": r["dt_id"], "phase": r["phase"],
            "window": [r["window_start"].isoformat(), r["window_end"].isoformat()],
            "issue": r["issue"], "severity": r["severity"], "residual_gap_kw": r["residual_gap_kw"],
            "local_actions": json.loads(r["local_actions"]), "recommended_action": r["recommended_action"],
            "evidence": json.loads(r["evidence"]), "status": r["status"],
            "status_changed_at": r["status_changed_at"].isoformat() if r["status_changed_at"] else None,
        }
        for r in rows
    ]


RECOMMENDATION_NEXT_STATUS = {"open": "acknowledged", "acknowledged": "dispatched", "dispatched": "resolved"}


@router.post("/api/recommendations/{run_id}/{rec_id}/advance")
async def advance_recommendation(run_id: str, rec_id: str) -> dict:
    """Moves a recommendation to its next state in the fixed sequence
    open -> acknowledged -> dispatched -> resolved (§8.5's action queue
    — "the only place LEO's control boundary becomes visible")."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM recommendation WHERE run_id = $1 AND rec_id = $2", run_id, rec_id
        )
        if row is None:
            raise HTTPException(404, f"no recommendation {rec_id!r} for run_id {run_id!r}")
        next_status = RECOMMENDATION_NEXT_STATUS.get(row["status"])
        if next_status is None:
            raise HTTPException(400, f"recommendation {rec_id!r} is already {row['status']!r}")
        await conn.execute(
            "UPDATE recommendation SET status = $3, status_changed_at = now() WHERE run_id = $1 AND rec_id = $2",
            run_id, rec_id, next_status,
        )
    return {"rec_id": rec_id, "status": next_status}
