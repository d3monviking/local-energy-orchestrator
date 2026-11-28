"""contracts/migrate.py — apply schema migrations on top of contracts/ddl.sql.

ddl.sql is the base schema, loaded once when the Postgres volume is first
created. Every change after that is a numbered file in
contracts/migrations/, named NNN_<owner>_<what>.sql (owner: a = Grid &
DISCOM, b = Household), applied in order exactly once per database and
recorded in `schema_migration`. A migration is never edited after it has
been merged; a fix is a new migration.

    POSTGRES_PORT=5433 python contracts/migrate.py      # apply pending
    POSTGRES_PORT=5433 python contracts/migrate.py --status
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
NAME = re.compile(r"^(\d{3})_[ab]_[a-z0-9_]+\.sql$")


def connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def migration_files() -> list[Path]:
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    bad = [p.name for p in files if not NAME.match(p.name)]
    if bad:
        raise SystemExit(f"badly named migration(s): {bad}; expected NNN_<a|b>_<what>.sql")
    numbers = [p.name[:3] for p in files]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    if dupes:
        raise SystemExit(f"two migrations share a number: {sorted(dupes)}; renumber the newer one")
    return files


def migrate(conn, verbose: bool = True) -> list[str]:
    """Apply every pending migration, each in its own transaction."""
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
                           name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        conn.commit()
        cur.execute("SELECT name FROM schema_migration")
        done = {r[0] for r in cur.fetchall()}
    applied = []
    for path in migration_files():
        if path.name in done:
            continue
        with conn.cursor() as cur:
            try:
                cur.execute(path.read_text())
                cur.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        applied.append(path.name)
        if verbose:
            print(f"applied {path.name}")
    return applied


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="list migrations and whether each is applied")
    args = ap.parse_args()
    conn = connect()
    if args.status:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('schema_migration') IS NOT NULL")
            done = set()
            if cur.fetchone()[0]:
                cur.execute("SELECT name FROM schema_migration")
                done = {r[0] for r in cur.fetchall()}
        for p in migration_files():
            print(f"{'applied' if p.name in done else 'PENDING'}  {p.name}")
        return
    applied = migrate(conn)
    print(f"{len(applied)} migration(s) applied" if applied else "schema up to date")
    conn.close()


if __name__ == "__main__":
    main()
