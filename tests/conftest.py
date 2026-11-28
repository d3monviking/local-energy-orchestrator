"""Shared fixtures: a Postgres connection with a built world (tests skip
without one, e.g. in CI) and an in-process fake SMS server."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.fixture(scope="session")
def db():
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
    c.rollback()
    from contracts.migrate import migrate
    migrate(c, verbose=False)
    yield c
    c.close()


def make_run(conn, run_id: str, note: str) -> None:
    """A throwaway run; drop_run removes it and everything under it."""
    drop_run(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO run (run_id, scenario, leo_enabled, seed, sim_start, sim_end, notes)
                       VALUES (%s, 'test', TRUE, 0, '2026-04-27T00:00Z', '2026-04-28T00:00Z', %s)""", (run_id, note))
    conn.commit()


def drop_run(conn, run_id: str) -> None:
    """Delete a run and its rows in every table with a run_id (older
    tables don't cascade)."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.columns
                       WHERE table_schema = 'public' AND column_name = 'run_id' AND table_name <> 'run'
                         AND table_name IN (SELECT table_name FROM information_schema.tables
                                            WHERE table_schema = 'public' AND table_type = 'BASE TABLE')""")
        tables = [r[0] for r in cur.fetchall()]
        for _ in range(len(tables) + 1):   # repeat: some of these tables reference each other
            for t in tables:
                cur.execute("SAVEPOINT s")
                try:
                    cur.execute(f'DELETE FROM "{t}" WHERE run_id = %s', (run_id,))
                    cur.execute("RELEASE SAVEPOINT s")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT s")
        cur.execute("DELETE FROM run WHERE run_id = %s", (run_id,))
    conn.commit()


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
    """SMS_URL pointed at a fake server; yields the list of requests it received."""
    server = HTTPServer(("127.0.0.1", 0), _FakeSms)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeSms.received = []
    monkeypatch.setenv("SMS_URL", f"http://127.0.0.1:{server.server_port}")
    yield _FakeSms.received
    server.shutdown()
