"""Golden figures: single-metric questions answered without the LLM must reproduce the K-eRP report values."""
import pytest

from app import metric_lookup
from app.semantic_layer import extract_entities
from conftest import run


def ask(db, q):
    return run(metric_lookup.answer(extract_entities(q), db, 10))


@pytest.mark.parametrize("q,expected", [
    ("What was RTPS's gross generation in FY 2024-25?", "8,310.351 MU"),       # RTPS MPR, FY
    ("RTPS gross generation in March 2025", "831.212 MU"),                        # RTPS MPR, March
    ("What was RTPS's PLF in March 2025?", "64.95%"),                             # RTPS MPR, March
    ("RTPS PLF in FY 2024-25", "55.16%"),                                          # RTPS MPR, FY
    ("RTPS generation on 06-Apr-2025", "26.245 MU"),                               # HO daily report
    ("RTPS PLF on 6 April 2025", "63.58%"),                                        # RTPS daily report
    ("RTPS-U2 generation on 06-Apr-2025", "4.008 MU"),                             # RTPS daily report
    ("RTPS unit 3 PLF on 06-Apr-2025", "83.93%"),                                  # RTPS daily report
    ("RTPS-U2 PLF in March 2025", "57.70%"),                                       # RTPS MPR
    ("SGS gross generation in March 2025", "526.981 MU"),                          # SGS MPR
    ("SGS generation on 06-Apr-2025", "14.177 MU"),                                # SGS daily report
    ("RTPS specific coal consumption in FY 2024-25", "0.786 kg/kWh"),             # RTPS MPR
    ("KPCL total gross generation in FY 2024-25", "37,602.179 MU"),               # workbook validation
    ("BTPS gross generation in FY 2024-25", "8,181.451 MU"),                       # workbook anchor
])
def test_golden(db, q, expected):
    a = ask(db, q)
    assert a is not None, q
    assert expected in a.answer, a.answer


def test_anchor_note(db):
    assert "pinned to the K-eRP report" in ask(db, "RTPS gross generation in March 2025").answer
    assert "pinned" not in ask(db, "BTPS gross generation in March 2025").answer


def test_relative_period_note(db):
    a = ask(db, "RTPS PLF last month")
    assert "64.95%" in a.answer and "30-Apr-2025" in a.answer


def test_partial_fy_note(db):
    assert "incomplete" in ask(db, "KPCL gross generation this FY").answer


def test_not_applicable(db):
    a = ask(db, "specific coal consumption of SGS in FY 2024-25")
    assert "hydro station" in a.answer and not a.sql


def test_no_data_before_commissioning(db):
    a = ask(db, "YCCPP gross generation in FY 2016-17")
    assert "no gross generation figure" in a.answer


@pytest.mark.parametrize("q", [
    "Which stations had the highest PLF in FY 2024-25?",       # ranking
    "Compare RTPS and BTPS PLF in FY 2024-25",                 # comparison / two stations
    "monthly trend of RTPS generation in FY 2024-25",          # trend
    "RTPS PLF and PAF in March 2025",                          # two metrics
    "RTPS PLF",                                                # no period
    "CEA target of RTPS on 06-Apr-2025",                       # monthly-only metric on a day
    "gross generation in FY 2024-25",                          # no place and no 'KPCL/total': never assume company
])
def test_falls_through_to_llm(db, q):
    assert ask(db, q) is None


def test_category_and_company_scopes(db):
    assert "The plant load factor (PLF) of thermal (coal) stations in FY 2024-25 was 51.18%" in ask(
        db, "PLF of thermal stations in FY 2024-25").answer
    assert "KPCL's KPCL share" not in ask(db, "KPCL share of state consumption in March 2025").answer
    assert "44.89%" in ask(db, "KPCL share of state consumption in March 2025").answer
