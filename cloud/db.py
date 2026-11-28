"""cloud/db.py — the API's Postgres connection pool, shared by every router."""

from __future__ import annotations

import os

import asyncpg

DB_DSN = (
    f"postgresql://{os.environ.get('POSTGRES_USER', 'leo')}:"
    f"{os.environ.get('POSTGRES_PASSWORD', 'leo')}@"
    f"{os.environ.get('POSTGRES_HOST', 'localhost')}:"
    f"{os.environ.get('POSTGRES_PORT', '5432')}/"
    f"{os.environ.get('POSTGRES_DB', 'leo')}"
)

_pool: asyncpg.Pool | None = None


async def open_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=5)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not open (the app's lifespan hasn't started)")
    return _pool
