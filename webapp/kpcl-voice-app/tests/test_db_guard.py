"""The database layer refuses writes and non-mart reads whatever the SQL looks like (no text check involved)."""
import sqlite3

import pytest

from app.db import RestrictedQueryError
from conftest import run


def test_mart_read_allowed(db):
    r = run(db.run("SELECT COUNT(*) FROM mart_dim_station", 10, 10, restricted=True))
    assert r.rows[0][0] == 33


def test_cte_subquery_and_json_each_allowed(db):
    sql = ("WITH t AS (SELECT station_key, gross_gen_mu FROM mart_fact_station_generation_fy WHERE fy = '2024-25') "
           "SELECT COUNT(*) FROM t WHERE station_key IN (SELECT value FROM json_each('[\"RTPS\",\"SGS\"]'))")
    assert run(db.run(sql, 10, 10, restricted=True)).rows[0][0] == 2


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM core_unit_daily_generation",
    "SELECT * FROM raw_units",
    "SELECT * FROM stg_units",
    "SELECT * FROM ledger_issue",
    "SELECT * FROM semantic_metric",
    "SELECT name FROM sqlite_master",
    "SELECT * FROM mart_dim_station WHERE station_key IN (SELECT station_code FROM core_station)",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
])
def test_non_mart_and_recursive_denied(db, sql):
    with pytest.raises(RestrictedQueryError):
        run(db.run(sql, 10, 10, restricted=True))


def test_writes_blocked_even_unrestricted(db):
    # The connection is mode=ro + query_only: SQLite itself refuses, with no text-level check in the way.
    for sql in ["DELETE FROM mart_dim_station", "UPDATE mart_dim_station SET station_name = 'x'",
                "CREATE TABLE t (x)", "DROP TABLE mart_dim_station"]:
        with pytest.raises(sqlite3.DatabaseError):
            run(db.run(sql, 10, 10))


def test_row_cap(db):
    r = run(db.run("SELECT date_key FROM mart_dim_date", 5, 10, restricted=True))
    assert len(r.rows) == 5 and r.truncated
