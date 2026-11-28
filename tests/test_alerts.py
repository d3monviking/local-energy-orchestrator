"""cloud/alerts.py: events and DR offers become household messages.

Needs Postgres with a built world (skips otherwise, e.g. in CI). Uses a
throwaway run, deleted afterwards, and an in-process fake SMS server.
"""

import json

import pytest

from tests.conftest import drop_run, make_run

RUN = "zz_test_alerts"
PREMISES: list = []   # backup premises, filled by the fixture
LATE: list = []       # the one restored in the later batch
CUT, BACK = "2026-04-27T14:10:00+00:00", "2026-04-27T15:40:00+00:00"


@pytest.fixture(scope="module")
def conn(db):
    c = db
    make_run(c, RUN, "tests/test_alerts.py")
    with c.cursor() as cur:
        cur.execute("SELECT household_id FROM premise_backup ORDER BY household_id")
        premises = [r[0] for r in cur.fetchall()]
        late, early = premises[0], premises[1:]

        def event(ts, kind, scope, payload):
            cur.execute("INSERT INTO event (run_id, ts, kind, scope, source, payload) VALUES (%s,%s,%s,%s,'test',%s)",
                        (RUN, ts, kind, scope, json.dumps(payload)))

        event(CUT, "grid_loss", "upstream", {"cause": "fault", "expected_end": BACK})
        event(CUT, "backup_start", "upstream",
              {"premises": [{"household_id": h, "allocated_kw": 0.0 if h == late else 1.5} for h in premises]})
        event(BACK, "grid_return", "upstream", {})
        event(BACK, "restore", "upstream", {"batch": 0, "household_ids": early})
        event("2026-04-27T15:43:00+00:00", "restore", "upstream", {"batch": 1, "household_ids": [late]})
        event("2026-04-27T15:41:00+00:00", "backup_end", "upstream", {})            # not alertable

        # A DR offer recorded with the pre-B1b bad timestamp (after its event).
        cur.execute("""INSERT INTO dr_event (run_id, id, phase, window_start, window_end, target_kw, v_paise_kwh)
                       VALUES (%s, 'EVT-T', 'Y', '2026-04-27T12:15Z', '2026-04-27T14:15Z', 5, 800)""", (RUN,))
        cur.execute("""INSERT INTO dr_offer (run_id, event_id, household_id, level, predicted_kwh, sent_at, channel)
                       VALUES (%s, 'EVT-T', 'HH-004', 50, 1.0, '2026-09-25T00:02Z', 'sms'),
                              (%s, 'EVT-T', 'HH-005', 0, 1.0, NULL, NULL)""", (RUN, RUN))
    c.commit()
    PREMISES[:] = premises
    LATE[:] = [late]
    yield c
    drop_run(c, RUN)


def _sms_kinds(conn, household_id):
    with conn.cursor() as cur:
        cur.execute("""SELECT kind, sent_at FROM message WHERE run_id = %s AND household_id = %s AND channel = 'sms'
                       ORDER BY sent_at, kind""", (RUN, household_id))
        return [r[0] for r in cur.fetchall()]


def test_dispatch_is_idempotent_and_targets_the_right_homes(conn, sms):
    from cloud.alerts import dispatch_alerts
    first = dispatch_alerts(conn, RUN)
    assert first > 0 and dispatch_alerts(conn, RUN) == 0

    ordinary = next(h for h in ("HH-000", "HH-001", "HH-002") if h not in PREMISES)
    assert _sms_kinds(conn, ordinary) == ["grid_loss", "grid_return"]
    for h in PREMISES:                         # backup premises: their own messages, not both
        assert _sms_kinds(conn, h) == ["backup_start", "restore"], h

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM message WHERE run_id = %s AND kind = 'backup_end'", (RUN,))
        assert cur.fetchone()[0] == 0


def test_offers_get_messages_and_bad_times_are_repaired(conn, sms):
    from cloud.alerts import dispatch_alerts
    dispatch_alerts(conn, RUN)
    with conn.cursor() as cur:
        cur.execute("SELECT household_id, sent_at FROM dr_offer WHERE run_id = %s ORDER BY household_id", (RUN,))
        offers = dict(cur.fetchall())
        cur.execute("""SELECT household_id, sent_at, delivered FROM message
                       WHERE run_id = %s AND kind = 'dr_offer' AND channel = 'sms'""", (RUN,))
        msgs = {h: (t, d) for h, t, d in cur.fetchall()}
    assert offers["HH-005"] is None and "HH-005" not in msgs          # not offered: no message
    assert offers["HH-004"].isoformat() == "2026-04-26T16:15:00+00:00"  # 20 h before 17:45 IST
    assert msgs["HH-004"] == (offers["HH-004"], True)


def test_zero_backup_allocation_says_none_available():
    from cloud.message_templates import render
    body = render("backup_start", "en", {"limit_a": 0, "limit_kw": 0}, CUT)
    assert "not available" in body and " A " not in body


def test_rerunning_a_dr_event_sends_nothing_and_never_messages_holdout(db, sms):
    import os
    from datetime import date, datetime, timezone

    import numpy as np

    from cloud.dr_engine.linucb import LinUCB
    from cloud.dr_engine.selection import ensure_default_consent, run_dr_event

    run = "zz_test_dr_rerun"
    make_run(db, run, "tests/test_alerts.py")
    ensure_default_consent(db)
    args = dict(event_id="EVT-RERUN", phase="Y",
                window_start=datetime(2026, 4, 27, 12, 15, tzinfo=timezone.utc),
                window_end=datetime(2026, 4, 27, 14, 15, tzinfo=timezone.utc),
                target_kw=8.0, v_rupees_per_kwh=8.0, event_date=date(2026, 4, 27), forecast_temp_c=35.0,
                hours_notice=20.0, sms_url=os.environ["SMS_URL"])
    try:
        first = run_dr_event(db, LinUCB(alpha=0.6), run, rng=np.random.default_rng(1), **args)
        n_sms = len(sms)
        again = run_dr_event(db, LinUCB(alpha=0.6), run, rng=np.random.default_rng(2), **args)
        assert first["n_sent"] > 0 and n_sms == first["n_sent"]
        assert again["n_sent"] == 0 and len(sms) == n_sms             # nothing re-sent
        with db.cursor() as cur:
            cur.execute("""SELECT count(*) FROM message m JOIN dr_offer o
                             ON o.run_id = m.run_id AND o.household_id = m.household_id
                           WHERE m.run_id = %s AND m.kind = 'dr_offer' AND o.is_holdout""", (run,))
            assert cur.fetchone()[0] == 0                                  # holdout never offered
            cur.execute("""SELECT count(*) FROM dr_offer WHERE run_id = %s AND sent_at IS NOT NULL""", (run,))
            assert cur.fetchone()[0] == first["n_sent"]
    finally:
        drop_run(db, run)
