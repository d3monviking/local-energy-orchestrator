"""cloud/routes/shared.py — read endpoints every console uses.

Owned jointly: change only by agreement between the Grid & DISCOM and the
Household streams (docs/WORK_PLAN.md). Feeder map, run catalogue, the
network result the map scrubs through, events, DR events, the ledger and
the household list.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException

from cloud import db, explain

router = APIRouter()

@router.get("/api/feeder/{dt_id}")
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
    async with db.pool().acquire() as conn:
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


@router.get("/api/runs")
async def list_runs() -> list[dict]:
    """Recorded runs the timeline can select between — the with/without-
    LEO toggle (Build Spec v1.0 §8.1/§8.3) is just two runs over the same
    seed/day, picked here by `leo_enabled`."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT run_id, scenario, leo_enabled, seed, sim_start, sim_end, created_at, notes FROM run ORDER BY created_at"
        )
    return [dict(r) for r in rows]


@router.get("/api/network_result/{run_id}")
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

    async with db.pool().acquire() as conn:
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


@router.get("/api/violation_episodes/{run_id}")
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
    async with db.pool().acquire() as conn:
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


@router.get("/api/run_catalog")
async def run_catalog() -> list[dict]:
    """Display metadata for the recorded runs that actually exist."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch("SELECT run_id, leo_enabled, sim_start, sim_end, notes FROM run")
    existing = {r["run_id"]: r for r in rows}
    return [
        {"run_id": rid, "label": meta["label"], "leo_enabled": meta["leo"],
         "sim_start": existing[rid]["sim_start"].isoformat(), "sim_end": existing[rid]["sim_end"].isoformat(),
         "notes": existing[rid]["notes"]}
        for rid, meta in explain.RUNS.items() if rid in existing
    ]


@router.get("/api/events/{run_id}")
async def events_for_run(run_id: str) -> list[dict]:
    """Event markers for the timeline (§8.1): overload trips, outage/
    restore, DR event start/end. Voltage violations themselves aren't
    stored as events — they're a per-interval property of
    `network_result` the map/timeline reads directly — this is only the
    discrete things that happened.
    """
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, kind, scope, source, payload FROM event WHERE run_id = $1 ORDER BY ts", run_id
        )
    return [
        {"ts": r["ts"].isoformat(), "kind": r["kind"], "scope": r["scope"], "source": r["source"],
         "payload": json.loads(r["payload"]) if r["payload"] else None}
        for r in rows
    ]


@router.get("/api/dr_events/{run_id}")
async def dr_events_for_run(run_id: str) -> list[dict]:
    """DR event + offer list for the operator's DR events screen
    (§8.3): incentive levels, expected kWh, holdout marked, verified kWh
    once settled."""
    async with db.pool().acquire() as conn:
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


@router.get("/api/ledger/{run_id}")
async def ledger_for_run(run_id: str) -> dict:
    """Settlement panel + unit economics (§8.3, §10.4): both are just
    queries against the ledger, by design."""
    async with db.pool().acquire() as conn:
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


@router.get("/api/households")
async def list_households() -> list[dict]:
    """Members list (§8.3): household registry, consent, critical
    registration, device-health stand-in (consent itself, since there's
    no live mock IoT health feed)."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, phase, sanctioned_load_kw, has_pv, is_business, is_critical, critical_class
               FROM household ORDER BY id"""
        )
    return [dict(r) for r in rows]
