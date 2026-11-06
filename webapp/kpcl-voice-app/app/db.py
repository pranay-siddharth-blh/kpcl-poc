"""
The one SQLite connection this app uses. Three independent protections, none trusted alone:

  1. The file is opened with `mode=ro` in its URI and `PRAGMA query_only = ON`: SQLite itself refuses
     every write, whatever the SQL says.
  2. For model-written SQL (restricted=True) an authorizer runs while each statement is compiled, so it
     holds however the SQL is phrased (aliases, sub-queries, CTEs): only SELECT / READ / FUNCTION actions
     are allowed; only mart_* tables may be read (never core_, raw_, stg_, semantic_, ledger_, src_,
     ref_ or SQLite's own catalogue); file / extension functions are denied.
  3. A wall-clock timeout interrupts the statement, and a row cap bounds the result.

Opened once in FastAPI's lifespan and shared through a single-worker executor + lock.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

ALLOWED_TABLE_PREFIX = "mart_"
# Table-valued functions show up as table reads; these two only parse JSON text.
_ALLOWED_VIRTUAL_TABLES = frozenset({"json_each", "json_tree"})
_DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "randomblob",
                               "zeroblob", "sqlite_offset"})
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _restricted_authorizer(action, arg1, arg2, _db_name, _trigger_or_view):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY  # writes, PRAGMA, ATTACH, recursive CTEs, transactions ...
    if action == sqlite3.SQLITE_READ:
        table = (arg1 or "").lower()
        if not (table.startswith(ALLOWED_TABLE_PREFIX) or table in _ALLOWED_VIRTUAL_TABLES):
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class RestrictedQueryError(Exception):
    """Model-written SQL tried to read or do something the authorizer forbids."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_seconds: float


class ReadOnlyDB:
    def __init__(self, db_path: Path):
        if not db_path.exists():
            raise FileNotFoundError(f"Warehouse not found at {db_path}. This app only reads it; build it with "
                                    "warehouse/build_warehouse.py.")
        self.path = db_path
        self._conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self._conn.execute("PRAGMA query_only = ON")
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kpcl-sqlite")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._conn.close()

    def run_sync(self, sql: str, max_rows: int, restricted: bool = False, params: tuple = ()) -> QueryResult:
        start = time.monotonic()
        with self._lock:
            if restricted:
                self._conn.set_authorizer(_restricted_authorizer)
            try:
                cur = self._conn.execute(sql, params)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
            except sqlite3.DatabaseError as exc:
                msg = str(exc).lower()
                if restricted and ("not authorized" in msg or "prohibited" in msg):
                    raise RestrictedQueryError(str(exc)) from exc
                raise
            finally:
                if restricted:
                    self._conn.set_authorizer(None)
        truncated = len(rows) > max_rows
        return QueryResult(columns, rows[:max_rows], truncated, time.monotonic() - start)

    async def run(self, sql: str, max_rows: int, timeout_seconds: float, restricted: bool = False,
                  params: tuple = ()) -> QueryResult:
        """restricted=True for all model-written SQL: the authorizer then applies."""
        future = self._executor.submit(self.run_sync, sql, max_rows, restricted, params)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self._conn.interrupt()
            raise TimeoutError(f"Query exceeded the {timeout_seconds}s timeout and was interrupted.")


_db: ReadOnlyDB | None = None


def get_db() -> ReadOnlyDB:
    if _db is None:
        raise RuntimeError("DB not initialised -- did FastAPI's lifespan run?")
    return _db


def init_db() -> ReadOnlyDB:
    global _db
    _db = ReadOnlyDB(settings.db_path)
    return _db


def shutdown_db() -> None:
    global _db
    if _db is not None:
        _db.close()
        _db = None
