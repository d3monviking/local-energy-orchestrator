"""cloud/recommendations.py — residual gaps and outages to structured,
evidence-backed DISCOM recommendations. Owner B. Build Specification
v1.0 §4.4, System Architecture v3.0 §13 (C13).

Reads what LEO's own closed loop (sim/loop.py) already recorded —
network_result for what the battery+DR plan did and didn't close,
event/premise_meter for an outage's scope and who's on backup — and
turns anything LEO cannot resolve locally into the fixed vocabulary
contracts/schemas/recommendation.schema.json defines. Generates nothing
from live computation: every recommendation here is evidenced by rows a
recorded run already wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg2.extras

# Table from System Architecture v3.0 §13 (C13).
ACTION_BY_ISSUE = {
    "forecast_overload": "rebalance_phase_load",
    "forecast_undervoltage": "tap_change_raise",  # §13: "Evening undervoltage -> raise tap"
    "forecast_overvoltage": "review_inverter_voltage_settings",
    "recurring_phase_imbalance": "rebalance_phase_load",
    "transformer_near_rating": "upgrade_planning",
    "outage": "flisr_scope",
}

# A phase violating more than this share of a day's intervals, even
# after the battery+DR plan ran, is "recurring" rather than a one-off —
# the threshold that turns a residual gap into a DISCOM-facing
# recommendation rather than something LEO just quietly absorbed less of.
RESIDUAL_MIN_INTERVALS = 4  # an hour or more out of limits with LEO running
V_LIMIT_PCT = 6.0  # neighbourhood.v_limit_pct


@dataclass
class RecommendationDraft:
    rec_id: str
    dt_id: str
    phase: str | None
    window_start: datetime
    window_end: datetime
    issue: str
    severity: str
    residual_gap_kw: float | None
    local_actions: list[str]
    recommended_action: str
    evidence: dict


def _next_rec_id(conn, window_start: datetime) -> str:
    cur = conn.cursor()
    date_str = window_start.strftime("%Y-%m-%d")
    cur.execute("SELECT count(*) FROM recommendation WHERE rec_id LIKE %s", (f"R-{date_str}-%",))
    n = cur.fetchone()[0]
    cur.close()
    return f"R-{date_str}-{n + 1:03d}"


def recommend_residual_violations(conn, run_id: str, dt_id: str, nominal_v_ln: float) -> list[RecommendationDraft]:
    """A phase still out of limits for an hour or more in the LEO-on run
    has a gap the battery+DR plan couldn't close locally — exactly the
    "residual goes to the recommendation engine" step (§14 escalation).

    Measured in TIME (intervals where any bus on the phase is out of
    limits), not as a share of bus x interval readings: the latter
    dilutes a 5-hour evening sag on the far end of a phase into a small
    percentage because the near-end buses stay fine, and stopped
    escalating real problems entirely once violations became evening-only.
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT phase::text, ts_end, min(voltage_v) AS min_v, max(voltage_v) AS max_v
           FROM network_result WHERE run_id = %s AND NOT is_forecast
           GROUP BY phase, ts_end HAVING bool_or(violation) ORDER BY phase, ts_end""",
        (run_id,),
    )
    rows = cur.fetchall()
    cur.close()

    floor = nominal_v_ln * (1 - V_LIMIT_PCT / 100)
    ceiling = nominal_v_ln * (1 + V_LIMIT_PCT / 100)
    drafts = []
    seq = 0
    # One escalation per phase AND direction: a sunny day can have both a
    # midday overvoltage and an evening undervoltage on the same phase, and
    # they need opposite DISCOM actions (lower vs raise tap).
    for phase in ("R", "Y", "B"):
        for issue, test, worst_of in (
            ("forecast_undervoltage", lambda r: r[2] < floor, lambda rs: min(r[2] for r in rs)),
            ("forecast_overvoltage", lambda r: r[3] > ceiling, lambda rs: max(r[3] for r in rs)),
        ):
            hits = [r for r in rows if r[0] == phase and test(r)]
            if len(hits) < RESIDUAL_MIN_INTERVALS:
                continue
            worst = worst_of(hits)
            gap_v = abs(worst - nominal_v_ln)
            severity = "high" if gap_v > 25 else ("medium" if gap_v > 18 else "low")
            window_start, window_end = hits[0][1], hits[-1][1]
            base = _next_rec_id(conn, window_start)
            rec_id = f"{base[:-3]}{int(base[-3:]) + seq:03d}"
            seq += 1
            drafts.append(RecommendationDraft(
                rec_id=rec_id, dt_id=dt_id, phase=phase, window_start=window_start - timedelta(minutes=15),
                window_end=window_end, issue=issue, severity=severity, residual_gap_kw=None,
                local_actions=["battery_dispatch", "dr_offers", "live_voltage_rule"],
                recommended_action=ACTION_BY_ISSUE[issue],
                evidence={
                    "hours_out_of_limits": len(hits) / 4, "worst_voltage_v": round(worst, 1),
                    "deviation_v": round(gap_v, 1), "nominal_v_ln": nominal_v_ln, "run_id": run_id,
                },
            ))
    return drafts

def recommend_outage_scope(conn, run_id: str, dt_id: str) -> list[RecommendationDraft]:
    """Turns an 'outage' run's grid_loss/backup_start events and critical-
    premise registry into the DISCOM's scope-and-crew-dispatch card
    (Build Spec v1.0 §7.6: "RE->>DIS: action card, dashboard + SMS/email").
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ts, kind, scope, payload FROM event WHERE run_id = %s AND kind IN ('grid_loss','grid_return','backup_start') ORDER BY ts",
        (run_id,),
    )
    rows = cur.fetchall()
    if not rows:
        cur.close()
        return []

    grid_loss = next((r for r in rows if r[1] == "grid_loss"), None)
    grid_return = next((r for r in rows if r[1] == "grid_return"), None)
    backup_start = next((r for r in rows if r[1] == "backup_start"), None)
    if grid_loss is None:
        cur.close()
        return []

    cur.execute(
        """SELECT h.id, h.critical_class, b.lat, b.lon
           FROM household h JOIN premise_backup pb ON pb.household_id = h.id
           JOIN bus b ON b.id = h.bus_id"""
    )
    critical_premises = [{"household_id": r[0], "critical_class": r[1]} for r in cur.fetchall()]
    cur.close()

    window_start = grid_loss[0]
    window_end = grid_return[0] if grid_return else window_start
    backup_payload = backup_start[3] if backup_start and backup_start[3] else {}

    rec_id = _next_rec_id(conn, window_start.replace(tzinfo=timezone.utc) if window_start.tzinfo is None else window_start)
    return [RecommendationDraft(
        rec_id=rec_id, dt_id=dt_id, phase=None, window_start=window_start, window_end=window_end,
        issue="outage", severity="high", residual_gap_kw=None,
        local_actions=["backup_circuit_energised"],
        recommended_action=ACTION_BY_ISSUE["outage"],
        evidence={
            "scope": grid_loss[2], "n_premises_on_backup": backup_payload.get("n_premises", 0),
            "backup_total_kw": backup_payload.get("total_kw", 0),
            "critical_premises": critical_premises, "run_id": run_id,
        },
    )]


def write_recommendations(conn, drafts: list[RecommendationDraft], run_id: str) -> int:
    cur = conn.cursor()
    n = 0
    for d in drafts:
        cur.execute(
            """INSERT INTO recommendation (run_id, rec_id, dt_id, phase, window_start, window_end,
                   issue, severity, residual_gap_kw, local_actions, recommended_action, evidence, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
               ON CONFLICT (run_id, rec_id) DO NOTHING""",
            (run_id, d.rec_id, d.dt_id, d.phase, d.window_start, d.window_end, d.issue, d.severity,
             d.residual_gap_kw, psycopg2.extras.Json(d.local_actions), d.recommended_action,
             psycopg2.extras.Json(d.evidence)),
        )
        n += cur.rowcount
    conn.commit()
    cur.close()
    return n


if __name__ == "__main__":
    import os
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )
    dt_id = "DT-0417"
    _cur = conn.cursor()
    _cur.execute("SELECT nominal_v_ln FROM neighbourhood WHERE dt_id = %s", (dt_id,))
    nominal_v_ln = float(_cur.fetchone()[0])  # 250 in this scenario; a hardcoded 230 mislabelled undervoltage as overvoltage
    _cur.close()

    for run_id in ("normal", "outage", "load_shedding", "surplus"):
        cur = conn.cursor()
        cur.execute("DELETE FROM recommendation WHERE run_id = %s", (run_id,))
        conn.commit()
        cur.close()

    cur = conn.cursor()
    cur.execute("SELECT run_id FROM run")
    existing = {r[0] for r in cur.fetchall()}
    cur.close()

    for run_id in ("normal", "surplus"):
        if run_id not in existing:
            continue
        residual = recommend_residual_violations(conn, run_id, dt_id, nominal_v_ln)
        print(f"residual-violation recommendations from {run_id!r}: {len(residual)}")
        for d in residual:
            print(f"  {d.rec_id}: phase {d.phase}, {d.issue}, severity={d.severity}, action={d.recommended_action}")
        write_recommendations(conn, residual, run_id)

    for run_id in ("outage", "load_shedding"):
        if run_id not in existing:
            continue
        outage_recs = recommend_outage_scope(conn, run_id, dt_id)
        print(f"outage recommendations from {run_id!r}: {len(outage_recs)}")
        write_recommendations(conn, outage_recs, run_id)

    conn.close()
