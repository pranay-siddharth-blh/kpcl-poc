"""Dashboard API: the headline figures agree with the chatbot's golden figures, and request values are whitelisted."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def get(client, **params):
    r = client.get("/api/dashboard", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_company_defaults_to_latest_complete_fy(client):
    d = get(client)
    assert d["fy"] == "2024-25" and d["prev_fy"] == "2023-24" and not d["is_partial"] and d["as_of"] == "2025-04-30"
    k = d["kpis"]
    assert round(k["cur"]["gen"], 3) == 37602.179                 # workbook validation figure
    assert k["cur"]["thermal_plf"] == 51.18                         # same as the metric lookup's thermal PLF
    assert k["units_out"] == 3 and {u["unit"] for u in d["units_out"]} == {"RTPS-U1", "RTPS-U4", "SIRWAR-U1"}
    assert len(d["stations"]) == 33 and len(d["monthly"]) == 12
    assert abs(sum(s["gen"] for s in d["stations"]) - k["cur"]["gen"]) < 1      # league adds up to the total
    assert abs(sum(m["gen"] for m in d["monthly"]) - k["cur"]["gen"]) < 0.01    # months add up to the year
    assert d["highlights"] and all(h["tone"] in ("good", "bad", "info") for h in d["highlights"])


def test_station_drill_down_matches_golden_figures(client):
    d = get(client, station="RTPS")
    assert d["scope"]["station"] == "RTPS"
    assert round(d["kpis"]["cur"]["gen"], 3) == 8310.351            # RTPS MPR, FY 2024-25
    assert d["kpis"]["cur"]["plf"] == 55.16
    march = next(m for m in d["monthly"] if m["month"].startswith("MAR"))
    assert round(march["gen"], 3) == 831.212 and march["plf"] == 64.95   # RTPS MPR, March 2025
    assert [u["unit"] for u in d["units"]][:2] == ["RTPS-U1", "RTPS-U2"] and len(d["units"]) == 8
    assert {u["unit"] for u in d["units_out"]} == {"RTPS-U1", "RTPS-U4"}
    assert [c["station"] for c in d["coal"]] == ["RTPS"] and d["reservoirs"] == []


def test_hydro_station_shows_its_reservoirs_not_coal(client):
    d = get(client, station="SGS")
    assert {r["name"] for r in d["reservoirs"]} == {"LINGANAMAKKI", "TALAKALALE"} and d["coal"] == []


def test_partial_year(client):
    d = get(client, fy="2025-26")
    assert d["is_partial"] and len(d["monthly"]) == 1


@pytest.mark.parametrize("params", [{"fy": "2099-00"}, {"station": "ZZTPS"}, {"station": "RTPS' OR '1'='1"},
                                    {"fy": "2024-25; DROP TABLE x"}, {"station": "x" * 50}])
def test_unknown_or_hostile_values_are_404(client, params):
    assert client.get("/api/dashboard", params=params).status_code == 404


def test_pages(client):
    for path in ("/", "/dashboard", "/chat"):
        r = client.get(path)
        assert r.status_code == 200 and "<html" in r.text.lower()
    assert "Generation Dashboard" in client.get("/").text and "chatScroll" in client.get("/chat").text


def test_insights_api_matches_the_dashboard(client):
    ins = client.get("/api/insights").json()
    dash = client.get("/api/dashboard").json()
    assert ins["fy"] == "2024-25" and len(ins["cards"]) >= 15
    cards = {c["topic"]: c for c in ins["cards"]}
    assert round(cards["Gross generation"]["value"], 3) == round(dash["kpis"]["cur"]["gen"], 3) == 37602.179
    assert cards["Thermal PLF"]["value"] == 51.18 and cards["Units out of service"]["value"] == 3
    assert all(c["tone"] in ("good", "bad", "info") for c in ins["cards"])
    assert sum(ins["summary"].values()) == len(ins["cards"])
    assert len(ins["stations_index"]) == 33


def test_insights_page_and_other_years(client):
    assert client.get("/insights").status_code == 200 and "Key insights" in client.get("/insights").text
    assert client.get("/api/insights", params={"fy": "2015-16"}).json()["fy"] == "2015-16"
    assert client.get("/api/insights", params={"fy": "2025-26"}).json()["is_partial"]
    assert client.get("/api/insights", params={"fy": "2099-00"}).status_code == 404


@pytest.mark.parametrize("params", [{}, {"station": "RTPS"}, {"fy": "2015-16"}, {"fy": "2019-20", "station": "SGS"}, {"fy": "2025-26"}])
def test_weekly_and_daily_add_up_to_the_year(client, params):
    d = get(client, **params)
    total = d["kpis"]["cur"]["gen"]
    assert abs(sum(x["gen"] for x in d["daily"]) - total) < 0.01
    assert abs(sum(x["gen"] for x in d["weekly"]) - total) < 0.01
    assert all(1 <= w["days"] <= 7 for w in d["weekly"]) and d["weekly"][0]["first"].endswith("-04-01")


def test_weekly_compares_like_with_like(client):
    d = get(client)                                          # FY 2024-25 against the same Monday-Sunday weeks of FY 2023-24
    w1 = d["weekly"][0]
    assert w1["first"] == "2024-04-01" and w1["days"] == 7 and w1["prev_days"] == 7
    assert d["daily"][0]["prev_date"] == "2023-04-01"          # same date, one year earlier


def test_station_without_data_in_a_year(client):
    d = get(client, fy="2020-21", station="YCCPP")             # commissioned July 2024
    assert d["daily"] == [] and d["weekly"] == []


def test_weeks_belong_to_the_month_holding_most_of_their_days(client):
    w = get(client)["weekly"]
    assert w[0]["month"] == "APR-2024"
    # 29 Apr - 05 May 2024: two days in April, five in May -> May
    assert next(x for x in w if x["first"] == "2024-04-29")["month"] == "MAY-2024"
    months = [x["month"] for x in w]
    assert months == sorted(months, key=lambda m: (m[-4:], ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"].index(m[:3])))
