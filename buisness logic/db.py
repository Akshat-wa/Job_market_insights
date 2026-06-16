# db.py
from typing import Tuple, List, Dict, Any
import contextlib
import sqlite3
import re

try:
    import psycopg2  # only needed when you switch to Postgres
except Exception:
    psycopg2 = None


def _sqlite_register_regex(con: sqlite3.Connection) -> None:
    """
    Register a REGEXP() SQL function on this sqlite3 connection.

    Usage in SQL:
      ... WHERE column REGEXP '(?i)(^|[^a-z])india([^a-z]|$)'
    """
    def _regex(expr: str, item: Any) -> int:
        if item is None:
            return 0
        try:
            # expr may already include (?i) etc.; we still pass through safely.
            return 1 if re.search(expr, str(item)) else 0
        except Exception:
            # On bad patterns or types, fail closed (no match)
            return 0

    try:
        con.create_function("REGEXP", 2, _regex)
    except Exception:
        # If function already exists or registration fails, keep going.
        pass


class DBAdapter:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    @contextlib.contextmanager
    def _conn(self, source: str):
        info = self.cfg[source]
        backend = info["backend"]
        dsn = info["dsn"]

        if backend == "sqlite":
            con = sqlite3.connect(dsn)
            # Optional: return rows as tuples (default) — no row_factory set.
            # Register REGEXP for this connection
            _sqlite_register_regex(con)
            try:
                yield con
            finally:
                con.close()

        elif backend == "postgres":
            if psycopg2 is None:
                raise RuntimeError("psycopg2 not installed. pip install psycopg2-binary")
            con = psycopg2.connect(dsn)
            try:
                yield con
            finally:
                con.close()

        else:
            raise ValueError(f"Unknown backend {backend}")

    def run_sql(self, source: str, sql: str, params: Tuple = ()) -> List[tuple]:
        with self._conn(source) as con:
            cur = con.cursor()
            cur.execute(sql, params)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []
            return rows
