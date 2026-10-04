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
from datetime import datetime, timezone

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
RESIDUAL_VIOLATION_SHARE_THRESHOLD = 0.15


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
    """A phase that's still violating a meaningful share of the day's
    intervals even in the LEO-on run has a gap the battery+DR plan
    couldn't close locally — exactly the "residual goes to the
    recommendation engine" step network_model.py's job 3 describes.
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT phase, count(*) FILTER (WHERE violation) AS n_violating, count(*) AS n_total,
                  min(voltage_v) AS worst_under, max(voltage_v) AS worst_over,
                  min(ts_end) AS window_start, max(ts_end) AS window_end
           FROM network_result WHERE run_id = %s GROUP BY phase""",
        (run_id,),
    )
    rows = cur.fetchall()
    cur.close()

    drafts = []
    for phase, n_violating, n_total, worst_under, worst_over, window_start, window_end in rows:
        share = n_violating / n_total if n_total else 0.0
        if share < RESIDUAL_VIOLATION_SHARE_THRESHOLD:
            continue

        undervolt_gap = nominal_v_ln - worst_under
        overvolt_gap = worst_over - nominal_v_ln
        if undervolt_gap >= overvolt_gap:
            issue, gap_v = "forecast_undervoltage", undervolt_gap
        else:
            issue, gap_v = "forecast_overvoltage", overvolt_gap

        severity = "high" if share > 0.5 else ("medium" if share > 0.3 else "low")
        rec_id = _next_rec_id(conn, window_start.replace(tzinfo=timezone.utc) if window_start.tzinfo is None else window_start)
        drafts.append(RecommendationDraft(
            rec_id=rec_id, dt_id=dt_id, phase=phase, window_start=window_start, window_end=window_end,
            issue=issue, severity=severity, residual_gap_kw=None,
            local_actions=["battery_dispatch", "dr_offers"],
            recommended_action=ACTION_BY_ISSUE[issue],
            evidence={
                "violating_intervals": n_violating, "total_intervals": n_total,
                "violation_share": round(share, 3), "worst_voltage_v": round(worst_under if issue == "forecast_undervoltage" else worst_over, 1),
                "nominal_v_ln": nominal_v_ln, "run_id": run_id,
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

    for run_id in ("normal", "outage"):
        cur = conn.cursor()
        cur.execute("DELETE FROM recommendation WHERE run_id = %s", (run_id,))
        conn.commit()
        cur.close()

    residual = recommend_residual_violations(conn, "normal", dt_id, nominal_v_ln)
    print(f"residual-violation recommendations from 'normal': {len(residual)}")
    for d in residual:
        print(f"  {d.rec_id}: phase {d.phase}, {d.issue}, severity={d.severity}, action={d.recommended_action}")
    n = write_recommendations(conn, residual, "normal")
    print(f"wrote {n} rows")

    outage_recs = recommend_outage_scope(conn, "outage", dt_id)
    print(f"\noutage recommendations from 'outage': {len(outage_recs)}")
    for d in outage_recs:
        print(f"  {d.rec_id}: {d.issue}, action={d.recommended_action}, evidence={d.evidence}")
    n = write_recommendations(conn, outage_recs, "outage")
    print(f"wrote {n} rows")

    conn.close()
