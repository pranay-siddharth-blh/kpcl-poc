"""MIS report (Excel): every figure in the workbook equals the dashboard's for the same selection and the warehouse's own
totals, the integrity checks on the Summary sheet pass for every FY, and requests are whitelisted like the dashboard's."""
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.config import settings
from app.main import app
from app.mis_report import build_mis_report
import asyncio


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def raw():
    c = sqlite3.connect(f"file:{settings.db_path.as_posix()}?mode=ro", uri=True)
    yield c
    c.close()


def report(client, **params):
    r = client.get("/api/mis-report", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "attachment" in r.headers["content-disposition"] and ".xlsx" in r.headers["content-disposition"]
    return load_workbook(io.BytesIO(r.content)), r


def rows(ws, header_first_cell):
    """The table under the header row whose first cell is `header_first_cell`, up to the first empty row."""
    out, on = [], False
    for row in ws.iter_rows(values_only=True):
        if row[0] == header_first_cell:
            on = True
            continue
        if on:
            if row[0] is None:
                break
            out.append(row)
    return out


def summary_kv(ws):
    return {r[1]: r for r in ws.iter_rows(values_only=True) if r[1] is not None}


def test_rtps_weekly_matches_dashboard_and_raw_data(client, raw):
    wb, r = report(client, fy="2023-24", station="RTPS", grain="weekly")
    assert "RTPS" in r.headers["content-disposition"] and "Weekly" in r.headers["content-disposition"]
    assert wb.sheetnames[0] == "Summary" and "Generation - Weekly" in wb.sheetnames and "Units" in wb.sheetnames
    assert "Generation - Monthly" not in wb.sheetnames          # only the grain that was asked for
    dash = client.get("/api/dashboard", params={"fy": "2023-24", "station": "RTPS"}).json()
    weeks = [x for x in rows(wb["Generation - Weekly"], "Week") if str(x[0]).startswith("W")]
    assert len(weeks) == len(dash["weekly"])
    for x, w in zip(weeks, dash["weekly"]):
        assert x[5] == pytest.approx(w["gen"], abs=1e-9) and x[8] == w["plf"] and x[4] == w["days"]
    # the workbook's year total is the warehouse's own figure
    (want,) = raw.execute("SELECT SUM(gross_gen_mu) FROM mart_fact_station_generation_fy WHERE fy='2023-24' AND station_key='RTPS'").fetchone()
    kv = summary_kv(wb["Summary"])
    assert kv["Gross generation"][3] == pytest.approx(want, abs=1e-6)
    assert kv["Gross generation"][3] == pytest.approx(8671.845, abs=1e-6)
    (prev,) = raw.execute("SELECT SUM(gross_gen_mu) FROM mart_fact_station_generation_fy WHERE fy='2022-23' AND station_key='RTPS'").fetchone()
    assert kv["Gross generation"][4] == pytest.approx(prev, abs=1e-6)
    assert kv["Gross generation"][5] == pytest.approx((want - prev) / prev * 100, abs=0.05)
    # the units add up to the station at full precision
    units = [u for u in rows(wb["Units"], "Unit") if str(u[0]).startswith("RTPS-U")]
    assert len(units) == 8 and sum(u[2] for u in units) == pytest.approx(want, abs=0.004)


@pytest.mark.parametrize("fy", ["2015-16", "2019-20", "2023-24", "2024-25", "2025-26"])
@pytest.mark.parametrize("station", [None, "RTPS", "SGS", "KALMALA"])
def test_integrity_checks_pass_for_every_fy_and_scope(db, fy, station):
    # straight to the builder: 20 HTTP calls would use up the per-IP rate limit the rest of the suite shares
    data, _ = asyncio.run(build_mis_report(db, fy, station, "all", settings.kpcl_query_timeout_seconds))
    wb = load_workbook(io.BytesIO(data))
    checks = [r for r in wb["Summary"].iter_rows(values_only=True) if r[2] in ("PASS", "CHECK")]
    assert len(checks) >= 4
    assert all(c[2] == "PASS" for c in checks), checks
    assert {"Generation - Monthly", "Generation - Weekly", "Generation - Daily"} <= set(wb.sheetnames)


def test_company_report_matches_dashboard_and_golden_figures(client, raw):
    wb, _ = report(client, fy="2024-25", grain="monthly")
    dash = client.get("/api/dashboard", params={"fy": "2024-25"}).json()
    assert round(summary_kv(wb["Summary"])["Gross generation"][3], 3) == 37602.179          # workbook validation figure
    months = [m for m in rows(wb["Generation - Monthly"], "Month") if m[0] != "FY total"]
    assert [round(m[1], 3) for m in months] == [m["gen"] for m in dash["monthly"]]
    # stations sheet: 33 stations, every one equal to the warehouse, and the subtotals add up
    st = [x for x in rows(wb["Stations"], "Station") if x[0] in {s["station"] for s in dash["stations"]}]
    assert len(st) == 33
    for x in st:
        (want,) = raw.execute("SELECT SUM(gross_gen_mu) FROM mart_fact_station_generation_fy WHERE fy='2024-25' AND station_key=?", (x[0],)).fetchone()
        assert x[4] == pytest.approx(want, abs=5e-4)
    grand = next(x for x in rows(wb["Stations"], "Station") if x[0] == "KPCL — all stations")
    assert grand[4] == pytest.approx(37602.179, abs=1e-3) and grand[7] == pytest.approx(dash["kpis"]["cur"]["plf"], abs=0.005)
    # station x month: RTPS March 2025 is the MPR figure
    mx = {r[0]: r for r in rows(wb["Station x Month"], "Station")}
    assert mx["RTPS"][13] == pytest.approx(831.212, abs=1e-3) and mx["RTPS"][14] == pytest.approx(8310.351, abs=1e-3)   # Mar-25, FY total


def test_part_year_is_flagged_not_compared(client):
    wb, _ = report(client, fy="2025-26", grain="monthly")
    ws = wb["Summary"]
    text = " ".join(str(c) for r in ws.iter_rows(values_only=True) for c in r if c)
    assert "part year" in text and "n/a (part year)" in text


def test_requests_are_whitelisted_and_defaults_work(client):
    for params in ({"fy": "2099-00"}, {"station": "NOPE"}, {"grain": "hourly"}, {"fy": "x" * 40}, {"grain": "y" * 40}):
        r = client.get("/api/mis-report", params=params)
        assert r.status_code == 404 and "xlsx" not in r.headers.get("content-disposition", "")
    r = client.get("/api/mis-report")            # defaults: latest complete FY, all stations, monthly
    assert r.status_code == 200 and "FY2024-25" in r.headers["content-disposition"]
