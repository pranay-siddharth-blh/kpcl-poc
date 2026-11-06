"""Every worked example runs, passes the SQL guard, and every verified answer renders; matching is conservative."""
import re

import pytest

from app import verified_queries as vq
from app.answer import result_table
from app.sql_safety import validate_readonly_sql
from conftest import run

EXAMPLES = vq.example_sql()


@pytest.mark.parametrize("n", sorted(EXAMPLES))
def test_example_runs_through_guard_and_authorizer(db, n):
    sql = validate_readonly_sql(EXAMPLES[n])
    r = run(db.run(sql, 500, 20, restricted=True))
    assert r.rows, f"example {n} returned nothing"


@pytest.mark.parametrize("entry", vq.REGISTRY, ids=lambda e: e.key)
def test_verified_renders(db, entry):
    r = run(db.run(entry.sql, 500, 20))
    text = entry.render([dict(zip(r.columns, x)) for x in r.rows], r.columns, r.rows)
    assert text.startswith("**") and not re.search(r"\b(None|nan|NaN)\b", text)
    result_table(r.columns, r.rows)
    if entry.chart:
        assert entry.chart.label_column in r.columns and entry.chart.value_column in r.columns


@pytest.mark.parametrize("q,key", [
    ("Which units are currently not running?", "v15_units_down_now"),
    ("which units are down right now", "v15_units_down_now"),
    ("RTPS back-down by reason in FY 2024-25", "v11_rtps_backdown"),
    ("Top 5 stations by PLF in FY 2024-25", "v25_top_plf_2425"),
    ("RTPS monthly performance report for March 2025", "v07_rtps_mpr_mar25"),
])
def test_match(q, key):
    m = vq.match(q)
    assert m is not None and m.key == key


@pytest.mark.parametrize("q", [
    "BTPS back-down by reason in FY 2024-25",          # different station
    "RTPS back-down by reason in FY 2023-24",          # different year
    "Bottom 5 stations by PLF in FY 2024-25",          # opposite direction
    "What was RTPS's PLF in March 2025?",               # a single figure, not a report
])
def test_no_false_match(q):
    assert vq.match(q) is None or vq.match(q).key not in ("v11_rtps_backdown", "v25_top_plf_2425")
