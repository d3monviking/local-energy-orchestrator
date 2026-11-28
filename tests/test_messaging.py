"""cloud/message_templates.py and cloud/messaging.notify (contracts/events.md, C-4).

Template tests are pure. notify tests need Postgres (POSTGRES_HOST/PORT);
they skip when it isn't reachable, e.g. in CI. They create a throwaway
run and delete it afterwards, and talk to an in-process fake SMS server.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cloud.message_templates import (
    TEMPLATES, TemplateError, fmt_day, fmt_hour, fmt_time, render, sms_segments,
)

# 27 Apr 2026: the load-shedding cut runs 19:40–21:10 IST = 14:10–15:40 UTC.
CUT_START, CUT_END = "2026-04-27T14:10:00+00:00", "2026-04-27T15:40:00+00:00"
NOTICE_AT = "2026-04-27T08:35:00+00:00"          # 14:05 IST, same day
OFFER_AT = "2026-04-26T18:00:00+00:00"           # 23:30 IST the night before
DR_START, DR_END = "2026-04-27T12:15:00+00:00", "2026-04-27T14:15:00+00:00"   # 17:45–19:45 IST

SAMPLE = {
    "load_shedding_notice": {"start": CUT_START, "end": CUT_END},
    "grid_loss": {"expected_end": CUT_END, "planned": True},
    "backup_start": {"limit_a": 16, "limit_kw": 4.0},
    "restore": {}, "grid_return": {}, "overload_trip": {},
    "dr_auto_shift": {"slot_ist": 10.75},
    "dr_auto_ac": {"start": DR_START, "end": DR_END, "payment_rs": 75},
    "dr_offer": {"start": DR_START, "end": DR_END, "level_rs": 50},
}


# ---- templates --------------------------------------------------------------

def test_times_are_ist_in_both_languages():
    assert fmt_time(CUT_START, "en") == "7:40 pm"
    assert fmt_time(CUT_START, "kn") == "ಸಂಜೆ 7:40"
    assert fmt_time(CUT_END, "kn") == "ರಾತ್ರಿ 9:10"
    assert fmt_time("2026-04-27T03:30:00+00:00", "en") == "9 am"
    assert fmt_hour(10.75, "en") == "10:45 am"


def test_day_words_are_relative_to_sending_time():
    assert fmt_day(DR_START, OFFER_AT, "en") == "tomorrow"           # offer at 23:30 IST the night before
    assert fmt_day(DR_START, OFFER_AT, "kn") == "ನಾಳೆ"
    assert fmt_day(DR_START, "2026-04-26T19:00:00+00:00", "en") == "today"   # 00:30 IST: already the 27th
    assert fmt_day(DR_START, "2026-04-20T08:00:00+00:00", "en") == "on 27 Apr"


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_every_template_renders_in_both_languages(kind):
    en = render(kind, "en", SAMPLE[kind], NOTICE_AT)
    kn = render(kind, "kn", SAMPLE[kind], NOTICE_AT)
    assert en.startswith("LEO: ") and kn.startswith("LEO: ")
    assert sms_segments(en) == 1, f"{kind} English is {len(en)} chars / not GSM-7: {en}"
    assert sms_segments(kn) <= 3, f"{kind} Kannada costs {sms_segments(kn)} segments"


def test_content_of_key_messages():
    notice = render("load_shedding_notice", "en", SAMPLE["load_shedding_notice"], NOTICE_AT)
    assert "today 7:40 pm-9:10 pm" in notice
    offer = render("dr_offer", "en", SAMPLE["dr_offer"], OFFER_AT)
    assert "Rs 50" in offer and "tomorrow" in offer
    appeal = render("dr_offer", "en", {**SAMPLE["dr_offer"], "level_rs": 0}, OFFER_AT)
    assert "Rs" not in appeal and "YES" in appeal
    assert "16 A" in render("backup_start", "en", SAMPLE["backup_start"], NOTICE_AT)


def test_template_errors():
    with pytest.raises(TemplateError):
        render("load_shedding_notice", "en", {"start": CUT_START}, NOTICE_AT)        # missing end
    with pytest.raises(TemplateError):
        render("no_such_kind", "en", {}, NOTICE_AT)
    with pytest.raises(TemplateError):
        render("grid_return", "hi", {}, NOTICE_AT)
    with pytest.raises(TemplateError):
        fmt_time("2026-04-27T14:10:00", "en")                                         # naive time


def test_sms_segments():
    assert sms_segments("a" * 160) == 1 and sms_segments("a" * 161) == 2
    assert sms_segments("€" * 80) == 1 and sms_segments("€" * 81) == 2               # extension chars count double
    assert sms_segments("ಕ" * 70) == 1 and sms_segments("ಕ" * 71) == 2               # Unicode: 70 / 67


# ---- notify (needs Postgres) -----------------------------------------------

RUN = "zz_test_messaging"


@pytest.fixture(scope="module")
def conn():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        c = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
            dbname=os.environ.get("POSTGRES_DB", "leo"), connect_timeout=2)
    except psycopg2.OperationalError:
        pytest.skip("Postgres not reachable")
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM household")
        if cur.fetchone()[0] == 0:
            pytest.skip("no world built in this database")
    from contracts.migrate import migrate
    migrate(c, verbose=False)
    with c.cursor() as cur:
        cur.execute("DELETE FROM run WHERE run_id = %s", (RUN,))
        cur.execute("""INSERT INTO run (run_id, scenario, leo_enabled, seed, sim_start, sim_end, notes)
                       VALUES (%s, 'test', TRUE, 0, '2026-04-27T00:00Z', '2026-04-28T00:00Z', 'tests/test_messaging.py')""",
                    (RUN,))
    c.commit()
    yield c
    with c.cursor() as cur:
        cur.execute("DELETE FROM run WHERE run_id = %s", (RUN,))   # cascades to message
    c.commit()
    c.close()


class _FakeSms(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeSms.received.append(body)
        out = json.dumps({"id": len(_FakeSms.received), "sent_at": body.get("sent_at")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.fixture()
def sms(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeSms)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeSms.received = []
    monkeypatch.setenv("SMS_URL", f"http://127.0.0.1:{server.server_port}")
    yield _FakeSms.received
    server.shutdown()


def _rows(conn, kind):
    with conn.cursor() as cur:
        cur.execute("""SELECT household_id, channel, delivered, error, body, sent_at FROM message
                       WHERE run_id = %s AND kind = %s ORDER BY household_id, channel""", (RUN, kind))
        return cur.fetchall()


def _phase_count(conn, phase):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM household WHERE phase = %s", (phase,))
        return cur.fetchone()[0]


def test_notify_delivers_sms_and_app_once_per_dedupe_key(conn, sms):
    from cloud.messaging import notify
    ids = notify(conn, RUN, "phase:Y", "grid_return", {}, at=CUT_END, dedupe_key="evt-1")
    n = _phase_count(conn, "Y")
    rows = _rows(conn, "grid_return")
    assert len(ids) == len(rows) == 2 * n
    assert all(r[2] for r in rows)                                    # sms and app delivered
    assert len(sms) == n and all(m["sent_at"] == "2026-04-27T15:40:00+00:00" for m in sms)
    assert notify(conn, RUN, "phase:Y", "grid_return", {}, at=CUT_END, dedupe_key="evt-1") == []
    assert len(_rows(conn, "grid_return")) == 2 * n and len(sms) == n


def test_sms_service_down_is_recorded_not_hidden(conn, monkeypatch):
    from cloud.messaging import notify
    monkeypatch.setenv("SMS_URL", "http://127.0.0.1:9")                # nothing listens on port 9
    notify(conn, RUN, "phase:R", "overload_trip", {}, at=CUT_START)
    rows = _rows(conn, "overload_trip")
    sms_rows = [r for r in rows if r[1] == "sms"]
    app_rows = [r for r in rows if r[1] == "app"]
    assert sms_rows and not any(r[2] for r in sms_rows) and all("unreachable" in r[3] for r in sms_rows)
    assert app_rows and all(r[2] for r in app_rows)


def test_backup_limits_are_per_premise(conn, sms):
    from cloud.messaging import notify
    with conn.cursor() as cur:
        cur.execute("SELECT household_id, max_current_a FROM premise_backup ORDER BY household_id")
        premises = cur.fetchall()
    per = {h: {"limit_a": a, "limit_kw": a * 250 / 1000} for h, a in premises}
    notify(conn, RUN, "backup_premises", "backup_start", {"per_household": per}, at=CUT_START, channels=("app",))
    rows = _rows(conn, "backup_start")
    assert len(rows) == len(premises)
    for h, _, _, _, body, _ in rows:
        assert f" {per[h]['limit_a']:g} A" in body


def test_bad_template_sends_nothing(conn, sms):
    from cloud.messaging import notify
    with pytest.raises(TemplateError):
        notify(conn, RUN, "all", "load_shedding_notice", {"start": CUT_START}, at=NOTICE_AT)   # no end
    assert _rows(conn, "load_shedding_notice") == [] and sms == []


def test_audience_errors(conn):
    from cloud.messaging import notify
    with pytest.raises(ValueError):
        notify(conn, RUN, "street:5", "grid_return", {}, at=CUT_END)
    with pytest.raises(ValueError):
        notify(conn, RUN, ["HH-NOPE"], "grid_return", {}, at=CUT_END)
    with pytest.raises(ValueError):
        notify(conn, RUN, "all", "grid_return", {}, at="2026-04-27T15:40:00")                # naive time
