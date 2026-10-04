"""cloud/dr_engine/selection.py — eligibility, holdout, fairness, ranking,
and event execution. Owner B. Build Specification v1.0 §5.4 / §11.3.

Turns "we need N kW off phase R, 19:00-21:00" into an actual list of SMS
offers: filters who may be asked, carves out a random holdout for
verification, scores everyone else with cloud/dr_engine/linucb.py, ranks
by expected profit with the 30-day fairness override, and sends offers
(via mocks/sms_service — the real interface a deployment's SMS gateway
sits behind) until the target is met or the eligible pool runs out.

Consent storage/enforcement for `dr_offers` lives here (Architecture
v3.0 §13, C14's split), not in cloud/api.py's HTTP surface yet — there's
no UI need for an endpoint until the citizen app's consent screen
(Day 5) exists to call it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from cloud.dr_engine.linucb import LinUCB, choose_level

MONTHLY_OFFER_CAP = 4
MIN_DAYS_SINCE_LAST_OFFER = 3
MIN_HISTORY_DAYS = 14
HOLDOUT_FRAC = 0.10
FAIRNESS_WINDOW_DAYS = 30

CONSENT_PURPOSES = ("meter_data_access", "dr_offers", "critical_premise_disclosure", "sensor_hosting")


def ensure_default_consent(conn) -> int:
    """Idempotent: every household starts with all four purposes granted
    until a household explicitly revokes one (the citizen app's demo
    beat, Build Spec v1.0 §7.5/§8.4). Returns the number of rows inserted.
    """
    cur = conn.cursor()
    cur.execute("SELECT id FROM household")
    household_ids = [row[0] for row in cur.fetchall()]
    inserted = 0
    for hh_id in household_ids:
        for purpose in CONSENT_PURPOSES:
            cur.execute(
                """INSERT INTO consent (household_id, purpose, granted)
                   VALUES (%s, %s, TRUE)
                   ON CONFLICT (household_id, purpose) DO NOTHING""",
                (hh_id, purpose),
            )
            inserted += cur.rowcount
    conn.commit()
    cur.close()
    return inserted


@dataclass
class EligibleHousehold:
    household_id: str
    typical_window_kw: float
    has_ac_or_cooler: bool
    has_pump: bool
    is_business: bool
    days_since_last_offer: float | None
    offers_this_month: int
    offers_received: int
    past_response_rate: float
    avg_verified_kwh: float
    fairness_priority: bool  # True if not offered in FAIRNESS_WINDOW_DAYS


def eligible_households(
    conn, phase: str, event_date, window_start_hour: int, window_end_hour: int, run_id: str | None = None,
) -> list[EligibleHousehold]:
    """Build Spec §11.3 step 1: phase, consent, >=14 days of history
    (household.enrolled_at — NULL, i.e. present since the world was
    built, counts as eligible), monthly cap, 3-day cooldown."""
    cur = conn.cursor()
    cur.execute(
        """SELECT h.id, h.is_business, h.sanctioned_load_kw,
                  bool_or(a.kind IN ('ac','cooler')) AS has_ac_or_cooler,
                  bool_or(a.kind = 'pump') AS has_pump
           FROM household h
           LEFT JOIN appliance a ON a.household_id = h.id
           WHERE h.phase = %s
             AND (h.enrolled_at IS NULL OR h.enrolled_at <= %s::date - (%s * INTERVAL '1 day'))
             AND h.id IN (
                 SELECT household_id FROM consent
                 WHERE purpose = 'dr_offers' AND granted
             )
           GROUP BY h.id, h.is_business, h.sanctioned_load_kw""",
        (phase, event_date, MIN_HISTORY_DAYS),
    )
    rows = cur.fetchall()

    month_start = event_date.replace(day=1)
    out = []
    for hh_id, is_business, sanctioned_load_kw, has_ac, has_pump in rows:
        cur.execute(
            """SELECT o.sent_at, o.replied, o.verified_kwh
               FROM dr_offer o JOIN dr_event e ON e.run_id = o.run_id AND e.id = o.event_id
               WHERE o.household_id = %s AND e.window_start >= %s
                 AND (%s::text IS NULL OR o.run_id = %s)
               ORDER BY o.sent_at DESC""",
            # Offer history is per recorded run: each run is its own world.
            # Counting across runs let the copies of one day's offers in the
            # outage/load-shedding runs hit the monthly cap for this one.
            (hh_id, month_start, run_id, run_id),
        )
        history = cur.fetchall()
        offers_this_month = len(history)
        if offers_this_month >= MONTHLY_OFFER_CAP:
            continue

        last_sent = history[0][0] if history else None
        days_since_last_offer = (
            (datetime.combine(event_date, datetime.min.time()) - last_sent.replace(tzinfo=None)).days
            if last_sent else None
        )
        if days_since_last_offer is not None and days_since_last_offer < MIN_DAYS_SINCE_LAST_OFFER:
            continue

        fairness_priority = days_since_last_offer is None or days_since_last_offer > FAIRNESS_WINDOW_DAYS

        cur.execute(
            """SELECT avg(import_kwh) FROM meter_interval
               WHERE household_id = %s
                 AND EXTRACT(HOUR FROM ts_end) BETWEEN %s AND %s""",
            (hh_id, window_start_hour, window_end_hour),
        )
        avg_window_kwh = cur.fetchone()[0]
        # Metered history is the real signal once there's a day-late
        # meter_interval file to read; the very first settled day has
        # none yet, so fall back to the household's own registered
        # sanctioned load — DISCOM GIS data, known from day zero, not a
        # flat constant that makes every residential household look
        # identical to the bandit except for a couple of binary flags.
        # 0.35 is a plausible evening-window utilisation fraction of a
        # household's sanctioned connection, not a DISCOM-published figure.
        typical_window_kw = float(avg_window_kwh) * 4 if avg_window_kwh else 0.35 * float(sanctioned_load_kw)

        n_replied = sum(1 for _, replied, _ in history if replied)
        past_response_rate = n_replied / len(history) if history else 0.0
        verified = [v for _, _, v in history if v is not None]
        avg_verified_kwh = sum(verified) / len(verified) if verified else 0.0

        out.append(EligibleHousehold(
            household_id=hh_id, typical_window_kw=typical_window_kw,
            has_ac_or_cooler=bool(has_ac), has_pump=bool(has_pump), is_business=bool(is_business),
            days_since_last_offer=float(days_since_last_offer) if days_since_last_offer is not None else None,
            offers_this_month=offers_this_month, offers_received=len(history),
            past_response_rate=past_response_rate, avg_verified_kwh=avg_verified_kwh,
            fairness_priority=fairness_priority,
        ))
    cur.close()
    return out


def run_dr_event(
    conn,
    bandit: LinUCB,
    run_id: str,
    event_id: str,
    phase: str,
    window_start: datetime,
    window_end: datetime,
    target_kw: float,
    v_rupees_per_kwh: float,
    event_date,
    forecast_temp_c: float,
    hours_notice: float,
    sms_url: str,
    rng,
    engagement_override: dict[str, dict] | None = None,
) -> dict:
    """Build Spec §11.3 steps 1-4, end to end: eligibility, 10% holdout,
    per-household level choice, rank (fairness first, then profit),
    send offers over mocks/sms_service until predicted reduction meets
    `target_kw` with margin or the pool is exhausted. Writes `dr_event`
    and `dr_offer` rows; returns a summary for the caller (sim/loop.py)
    to log and for the operator console to display.

    `engagement_override`: household_id -> engagement dict, from
    sim/loop.py's offline training run against these same real
    households (Build Spec §5.4: "trained offline over simulated
    history"). Without it, every household looks cold-start-identical
    on this, the programme's first-ever day — which is correct on day
    one of a real deployment, but means the bandit has had no chance yet
    to learn anything persona-specific. The offline training run stands
    in for that history existing already, same as a real bandit wouldn't
    be launched from scratch the morning of its first event either.
    """
    pool = eligible_households(conn, phase, event_date, window_start.hour, window_end.hour, run_id=run_id)
    rng.shuffle(pool)
    n_holdout = int(len(pool) * HOLDOUT_FRAC)
    holdout_ids = {hh.household_id for hh in pool[:n_holdout]}

    duration_hours = (window_end - window_start).total_seconds() / 3600.0
    event = {
        # Local (IST) hour, matching how sim/loop.py trains the bandit (19/24
        # for an evening event); window_start is UTC, so .hour alone served
        # the bandit 13/24 - a context it never saw in training.
        "start_hour_frac": ((window_start.hour + window_start.minute / 60 + 5.5) % 24) / 24.0,
        "duration_hours": duration_hours,
        "day_of_week_frac": window_start.weekday() / 7.0, "forecast_temp_c": forecast_temp_c,
        "hours_notice": hours_notice,
    }

    scored = []
    for hh in pool:
        household = {
            "typical_window_kw": hh.typical_window_kw, "window_variability": 0.3,
            "has_ac_or_cooler": float(hh.has_ac_or_cooler), "has_pump": float(hh.has_pump),
            "is_business": float(hh.is_business),
        }
        if engagement_override is not None and hh.household_id in engagement_override:
            engagement = engagement_override[hh.household_id]
        else:
            engagement = {
                "offers_received": hh.offers_received, "past_response_rate": hh.past_response_rate,
                "avg_verified_kwh": hh.avg_verified_kwh,
                "days_since_last_offer": hh.days_since_last_offer or 30.0,
                "offers_this_month": hh.offers_this_month,
            }
        choice = choose_level(bandit, household, engagement, event, v_rupees_per_kwh)
        scored.append((hh, choice))

    # Fairness first (not-offered-in-30-days households jump the queue),
    # profit within each tier — §11.3's fairness rule is a distinct
    # mechanism from the Stream 2 pooling rank (§12.4), kept separate here.
    scored.sort(key=lambda pair: (not pair[0].fairness_priority, -pair[1]["profit"]))

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO dr_event (run_id, id, phase, window_start, window_end, target_kw, v_paise_kwh)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id, id) DO NOTHING""",
        (run_id, event_id, phase, window_start, window_end, target_kw, round(v_rupees_per_kwh * 100)),
    )

    sent = []
    predicted_kw_total = 0.0
    for hh, choice in scored:
        is_holdout = hh.household_id in holdout_ids
        predicted_kwh = choice["predicted_kwh"]
        level = choice["level"]

        if not is_holdout and predicted_kw_total < target_kw * 1.1:
            message = _offer_message(level, predicted_kwh, v_rupees_per_kwh, window_start, window_end)
            try:
                resp = requests.post(
                    f"{sms_url}/send",
                    json={"to": hh.household_id, "channel": "sms", "body": message,
                          "meta": {"event_id": event_id, "level": level}},
                    timeout=3,
                )
                sent_at = resp.json().get("sent_at")
            except requests.RequestException:
                sent_at = datetime.utcnow().isoformat()
            predicted_kw_total += predicted_kwh / max(duration_hours, 1e-6)
        else:
            message = None
            sent_at = None

        cur.execute(
            """INSERT INTO dr_offer (run_id, event_id, household_id, level, predicted_kwh,
                   sent_at, channel, is_holdout)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, event_id, household_id) DO NOTHING""",
            (run_id, event_id, hh.household_id, level, predicted_kwh,
             sent_at, "sms" if sent_at else None, is_holdout),
        )
        sent.append({
            "household_id": hh.household_id, "level": level, "predicted_kwh": predicted_kwh,
            "is_holdout": is_holdout, "sent": sent_at is not None, "x": choice["x"],
        })

    conn.commit()
    cur.close()
    return {
        "event_id": event_id, "phase": phase, "target_kw": target_kw,
        "predicted_kw": predicted_kw_total, "n_eligible": len(pool), "n_holdout": n_holdout,
        "n_sent": sum(1 for s in sent if s["sent"]), "offers": sent,
    }


def _offer_message(level: float, predicted_kwh: float, v_rupees_per_kwh: float, start: datetime, end: datetime) -> str:
    window = f"{start.strftime('%-I%p').lower()}-{end.strftime('%-I%p').lower()}"
    if level == 0:
        return f"Please help avoid a local power cut tonight by cutting back {window}."
    rupees = round(level * v_rupees_per_kwh * predicted_kwh)
    return f"Earn about Rs {rupees} if you switch off your cooler {window} tonight."
