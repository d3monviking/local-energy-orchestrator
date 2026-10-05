"""cloud/api.py — FastAPI, serves the web app. Owner B. Build Spec v1.0 §4.4.

Day 1 scope was just booting the service. Day 2 adds the first real
endpoint: the feeder as GeoJSON, for the map component (§8.2). Everything
else (recorded-run listing, timeline data, plan approval, DR offers,
recommendations, ledger/settlement, consent) lands as the web app
surfaces that consume them get built.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import asyncpg

from cloud import explain
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DB_DSN = (
    f"postgresql://{os.environ.get('POSTGRES_USER', 'leo')}:"
    f"{os.environ.get('POSTGRES_PASSWORD', 'leo')}@"
    f"{os.environ.get('POSTGRES_HOST', 'localhost')}:"
    f"{os.environ.get('POSTGRES_PORT', '5432')}/"
    f"{os.environ.get('POSTGRES_DB', 'leo')}"
)

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=5)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="leo-cloud", lifespan=lifespan)

# Dev-mode CORS: the web app (localhost:3000) and this API (localhost:8030)
# are different origins. Access is read-only recorded-run data, nothing
# sensitive enough to warrant per-origin configuration in the prototype.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # GET for every read endpoint, POST for the real write actions
    # (plan approval, recommendation status advance, consent) — curl
    # doesn't enforce CORS preflight, so testing exclusively with curl
    # would never have caught this being GET-only.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "cloud"}


@app.get("/api/feeder/{dt_id}")
async def feeder_geojson(dt_id: str) -> dict:
    """The feeder as a GeoJSON FeatureCollection for the map component
    (Build Spec v1.0 §8.2): lines coloured by phase, households, sensors,
    the transformer, and the three battery blocks (co-located with the
    gateway at the transformer site — battery_block has no bus_id of its
    own, see contracts/ddl.sql).

    Voltage colouring (§8.2's "PathLayer coloured by voltage") is a
    per-run, per-timestamp quantity from `network_result`; it attaches
    once a recorded run exists for the timeline to scrub, not here.
    """
    async with pool.acquire() as conn:
        nb = await conn.fetchrow(
            "SELECT id, name, centroid_lat, centroid_lon, nominal_v_ln, v_limit_pct FROM neighbourhood WHERE dt_id = $1", dt_id
        )
        if nb is None:
            raise HTTPException(404, f"no neighbourhood for dt_id {dt_id}")

        transformer = await conn.fetchrow(
            "SELECT id, lat, lon FROM bus WHERE neighbourhood_id = $1 AND is_transformer", dt_id
        )

        lines = await conn.fetch(
            """SELECT l.id, l.from_bus, l.to_bus, l.conductor_type, l.length_m, l.ampacity_a,
                      fb.lat AS from_lat, fb.lon AS from_lon,
                      tb.lat AS to_lat, tb.lon AS to_lon
               FROM line l
               JOIN bus fb ON fb.id = l.from_bus
               JOIN bus tb ON tb.id = l.to_bus
               WHERE fb.neighbourhood_id = $1""",
            dt_id,
        )

        buses = await conn.fetch(
            "SELECT id, lat, lon, is_transformer FROM bus WHERE neighbourhood_id = $1", dt_id
        )

        households = await conn.fetch(
            """SELECT h.id, h.bus_id, b.lat, b.lon, h.phase, h.has_pv, h.is_business,
                      h.is_critical, h.critical_class
               FROM household h JOIN bus b ON b.id = h.bus_id
               WHERE b.neighbourhood_id = $1""",
            dt_id,
        )

        sensors = await conn.fetch(
            """SELECT s.id, b.lat, b.lon, s.phase, s.placement
               FROM sensor s JOIN bus b ON b.id = s.bus_id
               WHERE b.neighbourhood_id = $1""",
            dt_id,
        )

        batteries = await conn.fetch(
            "SELECT id, phase, unit_id, capacity_kwh, power_kw FROM battery_block"
        )

    features = []

    for ln in lines:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[ln["from_lon"], ln["from_lat"]], [ln["to_lon"], ln["to_lat"]]],
            },
            "properties": {
                "feature_type": "line",
                "id": ln["id"],
                "from_bus": ln["from_bus"],
                "to_bus": ln["to_bus"],
                "conductor_type": ln["conductor_type"],
                "length_m": ln["length_m"],
                "ampacity_a": ln["ampacity_a"],
            },
        })

    for b in buses:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [b["lon"], b["lat"]]},
            "properties": {
                "feature_type": "transformer" if b["is_transformer"] else "bus",
                "id": b["id"],
            },
        })

    for h in households:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [h["lon"], h["lat"]]},
            "properties": {
                "feature_type": "household",
                "id": h["id"],
                "bus_id": h["bus_id"],
                "phase": h["phase"],
                "has_pv": h["has_pv"],
                "is_business": h["is_business"],
                "is_critical": h["is_critical"],
                "critical_class": h["critical_class"],
            },
        })

    for s in sensors:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            "properties": {
                "feature_type": "sensor",
                "id": s["id"],
                "phase": s["phase"],
                "placement": s["placement"],
            },
        })

    if transformer is not None:
        for blk in batteries:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [transformer["lon"], transformer["lat"]]},
                "properties": {
                    "feature_type": "battery_block",
                    "id": blk["id"],
                    "phase": blk["phase"],
                    "unit_id": blk["unit_id"],
                    "capacity_kwh": blk["capacity_kwh"],
                    "power_kw": blk["power_kw"],
                },
            })

    return {
        "type": "FeatureCollection",
        "properties": {
            "dt_id": dt_id,
            "name": nb["name"],
            "centroid": [nb["centroid_lon"], nb["centroid_lat"]],
            "nominal_v_ln": nb["nominal_v_ln"],
            "v_limit_pct": nb["v_limit_pct"],
        },
        "features": features,
    }


@app.get("/api/runs")
async def list_runs() -> list[dict]:
    """Recorded runs the timeline can select between — the with/without-
    LEO toggle (Build Spec v1.0 §8.1/§8.3) is just two runs over the same
    seed/day, picked here by `leo_enabled`."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT run_id, scenario, leo_enabled, seed, sim_start, sim_end, created_at, notes FROM run ORDER BY created_at"
        )
    return [dict(r) for r in rows]


@app.get("/api/network_result/{run_id}")
async def network_result_at(run_id: str, ts_end: str, forecast: bool = False) -> dict:
    """Per-bus, per-phase voltage/loading/violation at the recorded
    timestamp nearest `ts_end` (ISO 8601) for this run — what the map's
    voltage colouring (§8.2) scrubs through. Snaps to the nearest
    available 15-minute interval rather than requiring an exact match,
    since the timeline component scrubs continuously.
    """
    ts_end_dt = datetime.fromisoformat(ts_end.replace("Z", "+00:00"))
    # forecast=true: what the day-ahead model PREDICTED for this interval
    # (stored once per forecast day under its source run), not what happened.
    src = explain.RUNS.get(run_id, {}).get("forecast_from", run_id) if forecast else run_id

    async with pool.acquire() as conn:
        nearest = await conn.fetchrow(
            """SELECT ts_end FROM network_result WHERE run_id = $1 AND is_forecast = $3
               ORDER BY abs(extract(epoch from (ts_end - $2::timestamptz))) LIMIT 1""",
            src, ts_end_dt, forecast,
        )
        if nearest is None:
            raise HTTPException(404, f"no network_result rows for run_id {run_id!r}")
        # No power-flow row within one interval means the feeder was dark
        # (outage): say so instead of showing the last energised state.
        if nearest["ts_end"] < ts_end_dt and (ts_end_dt - nearest["ts_end"]).total_seconds() >= 15 * 60:
            return {"run_id": run_id, "ts_end": None, "results": [], "de_energised": True}

        rows = await conn.fetch(
            """SELECT bus_id, phase, voltage_v, loading_pct, violation
               FROM network_result WHERE run_id = $1 AND ts_end = $2 AND is_forecast = $3""",
            src, nearest["ts_end"], forecast,
        )
    return {
        "run_id": run_id,
        "ts_end": nearest["ts_end"].isoformat(),
        "results": [dict(r) for r in rows],
    }


@app.get("/api/violation_episodes/{run_id}")
async def violation_episodes(run_id: str) -> list[dict]:
    """Per-phase voltage violations as contiguous episodes (start/end,
    worst voltage, over/under), not the raw per-15-minute-interval flag
    network_result carries. The operator console's violation banner and
    its "impacted phase" highlighting read this — a banner can't latch
    onto a boolean that flips every interval, it needs a start and an
    end. Computed with a classic gaps-and-islands grouping: collapse to
    one row per (phase, ts_end) first (several buses can share a
    timestamp), number the violating rows in ts_end order, and rows
    whose (ts_end - row_number * 15min) matches are contiguous.

    nominal_v_ln is read from `neighbourhood`, never a caller-supplied
    default — confirmed getting this wrong (230 instead of the
    scenario's real 250) silently flips every episode's over/under
    label and deviation sign, not just a display rounding error.
    """
    async with pool.acquire() as conn:
        nb = await conn.fetchrow("SELECT nominal_v_ln FROM neighbourhood LIMIT 1")
        nominal_v_ln = nb["nominal_v_ln"] if nb else 230.0
        rows = await conn.fetch(
            """
            WITH per_interval AS (
                SELECT phase, ts_end, bool_or(violation) AS violation,
                       max(voltage_v) AS max_v, min(voltage_v) AS min_v
                FROM network_result WHERE run_id = $1 AND is_forecast = false
                GROUP BY phase, ts_end
            ),
            flagged AS (
                SELECT phase, ts_end, max_v, min_v,
                       ts_end - (row_number() OVER (PARTITION BY phase ORDER BY ts_end) * interval '15 min') AS grp
                FROM per_interval WHERE violation
            )
            SELECT phase, min(ts_end) AS start_ts, max(ts_end) + interval '15 min' AS end_ts,
                   count(*) AS n_intervals, max(max_v) AS worst_over_v, min(min_v) AS worst_under_v
            FROM flagged GROUP BY phase, grp ORDER BY phase, start_ts
            """,
            run_id,
        )
    episodes = []
    for r in rows:
        over_dev = r["worst_over_v"] - nominal_v_ln
        under_dev = nominal_v_ln - r["worst_under_v"]
        is_over = over_dev >= under_dev
        episodes.append({
            "phase": r["phase"], "start_ts": r["start_ts"].isoformat(), "end_ts": r["end_ts"].isoformat(),
            "n_intervals": r["n_intervals"], "kind": "overvoltage" if is_over else "undervoltage",
            "worst_voltage_v": round(r["worst_over_v"] if is_over else r["worst_under_v"], 1),
            "deviation_v": round(over_dev if is_over else under_dev, 1),
        })
    return episodes


@app.get("/api/forecast/{run_id}")
async def forecast_for_run(run_id: str) -> dict:
    """The saved day-ahead forecast this run's plan was built from."""
    async with pool.acquire() as conn:
        return await explain.forecast(conn, run_id)


@app.get("/api/predicted_events/{run_id}")
async def predicted_events_for_run(run_id: str) -> list[dict]:
    """Events the forecast predicted (with reasons) next to what happened."""
    async with pool.acquire() as conn:
        return await explain.predicted_events(conn, run_id)


@app.get("/api/action_log/{run_id}")
async def action_log_for_run(run_id: str) -> list[dict]:
    """Every decision/action in time order, each with its reason."""
    async with pool.acquire() as conn:
        return await explain.action_log(conn, run_id)


@app.get("/api/run_catalog")
async def run_catalog() -> list[dict]:
    """Display metadata for the recorded runs that actually exist."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT run_id, leo_enabled, sim_start, sim_end, notes FROM run")
    existing = {r["run_id"]: r for r in rows}
    return [
        {"run_id": rid, "label": meta["label"], "leo_enabled": meta["leo"],
         "sim_start": existing[rid]["sim_start"].isoformat(), "sim_end": existing[rid]["sim_end"].isoformat(),
         "notes": existing[rid]["notes"]}
        for rid, meta in explain.RUNS.items() if rid in existing
    ]


@app.get("/api/impact")
async def impact(sweep_id: str | None = None) -> dict:
    """Reliability metrics and unit economics from the year-sampled sweep
    (eval/sweep.py + eval/economics.py), plus a per-month breakdown."""
    async with pool.acquire() as conn:
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


@app.get("/api/events/{run_id}")
async def events_for_run(run_id: str) -> list[dict]:
    """Event markers for the timeline (§8.1): overload trips, outage/
    restore, DR event start/end. Voltage violations themselves aren't
    stored as events — they're a per-interval property of
    `network_result` the map/timeline reads directly — this is only the
    discrete things that happened.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, kind, scope, source, payload FROM event WHERE run_id = $1 ORDER BY ts", run_id
        )
    return [
        {"ts": r["ts"].isoformat(), "kind": r["kind"], "scope": r["scope"], "source": r["source"],
         "payload": json.loads(r["payload"]) if r["payload"] else None}
        for r in rows
    ]


@app.get("/api/plan/{run_id}")
async def plan_for_run(run_id: str) -> dict:
    """The draft/approved battery plan (§8.3's Plan review screen):
    per-phase setpoint_kw by interval, plus the reserve floor and who
    approved it."""
    async with pool.acquire() as conn:
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


@app.post("/api/plan/{run_id}/{phase}/approve")
async def approve_plan_endpoint(run_id: str, phase: str, approved_by: str = "operator-1") -> dict:
    """The Approve button (§8.3): "the single most important interaction
    in the demo... a human is accountable for what the operator's
    equipment does." Idempotent — intervals already approved (every
    plan sim/loop.py writes is pre-approved, matching the locked
    pre-recorded-video decision) are left as they were.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
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


@app.post("/api/plan/{run_id}/approve")
async def approve_whole_plan(run_id: str, approved_by: str = "operator-1") -> dict:
    """Approve all three phases in one decision (the Plan review screen's
    single Approve). The approval is stamped at the run's own 23:30 IST
    slot on D-1, not wall-clock now(), so replaying a recorded day keeps
    "plan approved" and "DR offers sent" in the right place on the timeline."""
    async with pool.acquire() as conn:
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


@app.post("/api/plan/{run_id}/withdraw")
async def withdraw_plan(run_id: str, by: str = "operator-1") -> dict:
    """Withdraw approval (undo, or reset the demo). Nothing reaches the
    batteries from an unapproved plan."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE plan SET approved_at = NULL, approved_by = NULL WHERE run_id = $1", run_id)
        await _record_decision(conn, run_id, "withdrawn", by)
    return {"run_id": run_id, "status": "pending"}


@app.post("/api/plan/{run_id}/reject")
async def reject_plan(run_id: str, reason: str = Body(..., embed=True), by: str = "operator-1") -> dict:
    """Reject the plan, with the operator's reason. The batteries get no
    schedule from it and no DR messages go out; the reason is kept with
    the decision so the plan can be revisited."""
    reason = reason.strip()
    if not reason:
        raise HTTPException(422, "a reason is required to reject a plan")
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM plan WHERE run_id = $1 LIMIT 1", run_id) is None:
            raise HTTPException(404, f"no plan for run {run_id!r}")
        await conn.execute("UPDATE plan SET approved_at = NULL, approved_by = NULL WHERE run_id = $1", run_id)
        await _record_decision(conn, run_id, "rejected", by, reason)
    return {"run_id": run_id, "status": "rejected", "reason": reason}


@app.get("/api/dr_events/{run_id}")
async def dr_events_for_run(run_id: str) -> list[dict]:
    """DR event + offer list for the operator's DR events screen
    (§8.3): incentive levels, expected kWh, holdout marked, verified kWh
    once settled."""
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, phase, window_start, window_end, target_kw, v_paise_kwh FROM dr_event WHERE run_id = $1",
            run_id,
        )
        out = []
        for ev in events:
            offers = await conn.fetch(
                """SELECT household_id, level, predicted_kwh, sent_at, channel, replied, is_holdout, verified_kwh
                   FROM dr_offer WHERE run_id = $1 AND event_id = $2 ORDER BY household_id""",
                run_id, ev["id"],
            )
            out.append({
                "event_id": ev["id"], "phase": ev["phase"],
                "window_start": ev["window_start"].isoformat(), "window_end": ev["window_end"].isoformat(),
                "target_kw": ev["target_kw"], "v_paise_kwh": ev["v_paise_kwh"],
                "n_offers": len(offers), "n_sent": sum(1 for o in offers if o["sent_at"]),
                "n_accepted": sum(1 for o in offers if o["replied"]),
                "n_holdout": sum(1 for o in offers if o["is_holdout"]),
                "offers": [dict(o) | {"sent_at": o["sent_at"].isoformat() if o["sent_at"] else None} for o in offers],
            })
    return out


@app.get("/api/recommendations/{run_id}")
async def recommendations_for_run(run_id: str) -> list[dict]:
    """The DISCOM action queue (§8.5) and the operator's residual-gap
    view both read this — same rows, different surface."""
    async with pool.acquire() as conn:
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


@app.post("/api/recommendations/{run_id}/{rec_id}/advance")
async def advance_recommendation(run_id: str, rec_id: str) -> dict:
    """Moves a recommendation to its next state in the fixed sequence
    open -> acknowledged -> dispatched -> resolved (§8.5's action queue
    — "the only place LEO's control boundary becomes visible")."""
    async with pool.acquire() as conn:
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


@app.get("/api/ledger/{run_id}")
async def ledger_for_run(run_id: str) -> dict:
    """Settlement panel + unit economics (§8.3, §10.4): both are just
    queries against the ledger, by design."""
    async with pool.acquire() as conn:
        by_stream = await conn.fetch(
            """SELECT entry_type, count(*) AS n_entries, sum(amount_paise) AS total_paise
               FROM ledger WHERE run_id = $1 GROUP BY entry_type""",
            run_id,
        )
        rows = await conn.fetch(
            """SELECT household_id, date, entry_type, deficit_kwh, matched_kwh, amount_paise,
                      payout_scaling_factor, linked_event_id, running_balance_paise
               FROM ledger WHERE run_id = $1 ORDER BY household_id, date""",
            run_id,
        )
    return {
        "run_id": run_id,
        "by_stream": [dict(r) for r in by_stream],
        "total_paise": sum(r["total_paise"] or 0 for r in by_stream),
        "n_households_paid": len({r["household_id"] for r in rows}),
        "rows": [dict(r) | {"date": r["date"].isoformat()} for r in rows],
    }


@app.get("/api/households")
async def list_households() -> list[dict]:
    """Members list (§8.3): household registry, consent, critical
    registration, device-health stand-in (consent itself, since there's
    no live mock IoT health feed)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, phase, sanctioned_load_kw, has_pv, is_business, is_critical, critical_class
               FROM household ORDER BY id"""
        )
    return [dict(r) for r in rows]


@app.get("/api/consent/{household_id}")
async def get_consent(household_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT purpose, granted, changed_at FROM consent WHERE household_id = $1 ORDER BY purpose",
            household_id,
        )
    return [{"purpose": r["purpose"], "granted": r["granted"], "changed_at": r["changed_at"].isoformat()} for r in rows]


@app.post("/api/consent/{household_id}/{purpose}")
async def set_consent(household_id: str, purpose: str, granted: bool) -> dict:
    """The citizen app's consent screen (§8.4). Revoking `dr_offers` here
    is what /api/dr_eligibility checks against — the value is the
    consequence (dropping out of the next event), not the toggle itself.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO consent (household_id, purpose, granted, changed_at) VALUES ($1,$2,$3,now())
               ON CONFLICT (household_id, purpose) DO UPDATE SET granted = $3, changed_at = now()""",
            household_id, purpose, granted,
        )
    return {"household_id": household_id, "purpose": purpose, "granted": granted}


@app.get("/api/dr_eligibility/{household_id}")
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


@app.get("/api/dispatch/{run_id}")
async def dispatch_for_run(run_id: str) -> list[dict]:
    """Live operations view (§8.3): SoC across the three blocks, mode,
    live-rule triggers, over the day."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT block_id, ts_end, setpoint_kw, actual_kw, soc_after, mode, rule_triggered
               FROM dispatch WHERE run_id = $1 ORDER BY block_id, ts_end""",
            run_id,
        )
    return [dict(r) | {"ts_end": r["ts_end"].isoformat()} for r in rows]


@app.get("/api/sensor_readings/{run_id}")
async def sensor_readings_for_run(run_id: str) -> list[dict]:
    """Per-phase voltage traces (§8.3's live operations view)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT sensor_id, ts_end, voltage_v, supply_present FROM sensor_reading
               WHERE run_id = $1 ORDER BY sensor_id, ts_end""",
            run_id,
        )
    return [dict(r) | {"ts_end": r["ts_end"].isoformat()} for r in rows]


@app.get("/api/backup_households/{run_id}")
async def backup_households_for_run(run_id: str) -> list[dict]:
    """Households actually served backup power during this run — the
    map's "households on the backup circuit are visually distinct"
    requirement (§8.2), for the outage run's event replay. Returns
    bus_id alongside household_id since that's what the map indexes by.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT pm.household_id, h.bus_id FROM premise_meter pm
               JOIN household h ON h.id = pm.household_id WHERE pm.run_id = $1""",
            run_id,
        )
    return [dict(r) for r in rows]


@app.get("/api/community/{run_id}")
async def community_stats(run_id: str) -> dict:
    """The citizen app's community page (§8.4): battery status, outage
    minutes avoided, total payouts to the neighbourhood."""
    async with pool.acquire() as conn:
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


@app.get("/api/citizen/{household_id}")
async def citizen_summary(household_id: str) -> dict:
    """Bundles everything the citizen app's screens need (§8.4) in one
    call: today's offer, earnings by stream, day-late usage, outage/
    backup status, consent — same underlying rows the operator and
    DISCOM surfaces read, just household-scoped.
    """
    async with pool.acquire() as conn:
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
