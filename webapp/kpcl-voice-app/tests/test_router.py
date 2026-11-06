"""Deterministic entity extraction: places, periods (incl. relative dates), metrics, intents."""
import pytest

from app.semantic_layer import definitional_term, extract_entities, is_identity_question


def periods(q):
    return [(p["kind"], p["value"]) for p in extract_entities(q).get("periods", [])]


@pytest.mark.parametrize("q,expected", [
    ("RTPS PLF in March 2025", [("month", "MAR-2025")]),
    ("generation in Mar-25", [("month", "MAR-2025")]),
    ("generation on 06-Apr-2025", [("date", "2025-04-06")]),
    ("generation on 6 April 2025", [("date", "2025-04-06")]),
    ("generation on 2025-04-06", [("date", "2025-04-06")]),
    ("generation on 06/04/2025", [("date", "2025-04-06")]),
    ("generation in FY 2024-25", [("fy", "2024-25")]),
    ("generation in FY24-25", [("fy", "2024-25")]),
    ("generation in FY25", [("fy", "2024-25")]),
    ("generation in 2024-25", [("fy", "2024-25")]),
    ("SGS in water year 2024-25", [("water_year", "2024-25")]),
    ("PLF in Q1 FY 2024-25", [("quarter", "Q1")]),
    ("last month", [("month", "MAR-2025")]),
    ("this month", [("month", "APR-2025")]),
    ("last FY", [("fy", "2024-25")]),
    ("this financial year", [("fy", "2025-26")]),
    ("today", [("date", "2025-04-30")]),
    ("generation in March 2014", [("out_of_range", "MAR-2014")]),
])
def test_periods(q, expected):
    assert periods(q) == expected


def test_quarter_carries_its_fy():
    p = extract_entities("PLF in Q1 FY 2024-25")["periods"][0]
    assert p["fy"] == "2024-25"


@pytest.mark.parametrize("q,key,value", [
    ("What was RTPS's PLF in March 2025?", "stations", ["RTPS"]),
    ("Raichur thermal plf in mar 2025", "stations", ["RTPS"]),
    ("Bellary generation", "stations", ["BTPS"]),
    ("RTPS-U2 generation", "units", ["RTPS-U2"]),
    ("RTPS unit 8 generation", "units", ["RTPS-U8"]),
    ("unit 3 of BTPS outages", "units", ["BTPS-U3"]),
    ("PLF of thermal stations", "categories", ["THERMAL-COAL"]),
    ("minor hydro generation", "categories", ["MINOR HYDRO"]),
    ("hydro generation", "plant_type", "HYDRO"),
    ("KPCL total generation", "scope", "company"),
    ("Linganamakki dam level", "reservoirs", ["LINGANAMAKKI"]),
    ("Supa dam storage", "reservoirs", ["SUPA"]),
])
def test_places(q, key, value):
    assert extract_entities(q).get(key) == value


def test_shared_dam_and_station_name():
    e = extract_entities("Supa generation last month")
    assert e.get("stations") == ["SUPA"] and "reservoirs" not in e
    e = extract_entities("Supa dam level on 06-Apr-2025")
    assert e.get("reservoirs") == ["SUPA"] and "SUPA" not in e.get("stations", [])


def test_station_name_words_are_not_categories():
    e = extract_entities("Raichur Thermal Power Station PLF in FY 2024-25")
    assert e.get("stations") == ["RTPS"] and "categories" not in e


@pytest.mark.parametrize("q,metrics", [
    ("RTPS PLF", ["plf"]), ("RTPS PLF with NLD", ["plf_with_nld"]), ("aux % of RTPS", ["aux_pct"]),
    ("specific coal consumption", ["specific_coal"]), ("coal consumption at BTPS", ["coal_consumption"]),
    ("CEA target achievement", ["cea_target_achievement"]), ("gross generation and PLF", ["gross_generation", "plf"]),
])
def test_metrics(q, metrics):
    assert extract_entities(q).get("metrics") == metrics


def test_intents():
    assert "ranking" in extract_entities("Which stations had the highest PLF?")["intents"]
    assert "comparison" in extract_entities("Compare BTPS and YTPS")["intents"]
    assert "trend" in extract_entities("monthly trend of generation")["intents"]
    assert "intents" not in extract_entities("RTPS PLF in March 2025")


def test_identity_and_definitions():
    assert is_identity_question("who are you?")
    assert definitional_term("What is PAF?") == "PAF"
    assert definitional_term("what does NLD mean") == "NLD"
    assert definitional_term("What is RTPS PLF in March 2025?") is None
