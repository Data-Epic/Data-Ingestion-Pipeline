"""
Database connection management (Postgres).

Kept separate from repository.py so that:
- Tests can swap in a test DB / mock connection easily.
- Connection pooling / lifecycle (open on startup, close on shutdown)
  lives in exactly one place, wired into FastAPI's lifespan events.

TODO (next pass):
- Pick sync psycopg (simpler, fine since DB calls happen inside an
  already-async background task) OR asyncpg (fully async end-to-end).
  Given you're still solidifying async, sync psycopg + a thread-pool
  wrapper is the gentler path - worth discussing before locking this in.
- get_connection() / connection pool setup using DATABASE_URL from
  src/config.py
- A small helper for "run this SQL with these params" that ALWAYS uses
  parameterized queries (never f-string SQL) - this is what
  repository.py will call.
"""



from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from src.config import get_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily create a single process-wide connection pool."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=True,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """
    Borrow a connection from the pool for the duration of a `with` block.

    Always go through this - never call psycopg.connect() directly
    elsewhere - so pooling and cleanup stay centralized in this one file.

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...", (param,))
    """
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    """Call this from FastAPI's shutdown lifespan event."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
