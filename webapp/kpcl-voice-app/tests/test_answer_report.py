"""AI-mode "Download report": which answers get it, and the workbook holds exactly the answer's figures.

A station / KPCL summary for an FY downloads the dashboard's MIS workbook for that selection; any other report answer
downloads an answer workbook whose Data sheet is the answer's own result, row for row."""
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import answer_report
from app.config import settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def raw():
    c = sqlite3.connect(f"file:{settings.db_path.as_posix()}?mode=ro", uri=True)
    yield c
    c.close()


def ask(client, q):
    r = client.post("/api/ask_once", json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


def download(client, rid):
    r = client.get(f"/api/answer-report/{rid}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return load_workbook(io.BytesIO(r.content)), r.headers["content-disposition"]


def cells(ws):
    return [c for row in ws.iter_rows(values_only=True) for c in row if c is not None]


def test_station_fy_report_is_the_dashboard_mis(client, raw):
    a = ask(client, "give me a report of rtps for financial year 2024-25")
    assert a["answerable"] and a["report_id"]
    wb, cd = download(client, a["report_id"])
    assert "KPCL_MIS_RTPS_FY2024-25" in cd and wb.sheetnames[0] == "Summary"
    kv = {r[1]: r for r in wb["Summary"].iter_rows(values_only=True) if r[1] is not None}
    assert "give me a report of rtps" in kv["Requested in AI mode"][2]
    (want,) = raw.execute("SELECT SUM(gross_gen_mu) FROM mart_fact_station_generation_fy WHERE fy='2024-25' AND station_key='RTPS'").fetchone()
    assert kv["Gross generation"][3] == pytest.approx(want, abs=1e-6)
    assert "8,310.4 MU" in a["answer"]      # the chat answer and the workbook are the same figure
    assert all(r[2] == "PASS" for r in wb["Summary"].iter_rows(values_only=True) if r[2] in ("PASS", "CHECK"))


@pytest.mark.parametrize("q,grain,sheet", [
    ("Give me a weekly report of RTPS for FY 2023-24", "Weekly", "Generation - Weekly"),
    ("Daily MIS report for SGS FY 2024-25", "Daily", "Generation - Daily"),
    ("Complete report of KPCL for FY 2023-24", "All", "Station x Month"),
])
def test_period_words_pick_the_mis_sheets(client, q, grain, sheet):
    a = ask(client, q)
    assert a["answerable"] and a["report_id"], a["answer"]
    wb, cd = download(client, a["report_id"])
    assert f"_{grain}.xlsx" in cd and sheet in wb.sheetnames


def test_scorecard_report_data_sheet_is_the_answer_result(client, raw):
    a = ask(client, "Performance report of all thermal stations in FY 2023-24")
    assert a["report_id"]
    wb, _ = download(client, a["report_id"])
    assert wb.sheetnames == ["Report", "Data", "Chart", "Notes"]
    rep = cells(wb["Report"])
    assert any("Performance report of all thermal stations" in str(c) for c in rep)
    assert any(str(c).startswith("RTPS — 8,671.845 MU") for c in rep)      # findings exactly as answered
    data = [r for r in wb["Data"].iter_rows(values_only=True)][5:]
    got = {r[0]: r for r in data if r[0] in ("RTPS", "BTPS", "YTPS")}
    assert len(got) == 3
    for st, row in got.items():
        (want,) = raw.execute("SELECT gross_gen_mu FROM mart_fact_station_generation_fy WHERE fy='2023-24' AND station_key=?", (st,)).fetchone()
        assert row[4] == pytest.approx(want, abs=5e-4)
    assert len(data) >= len(a["rows"]) and [r[0] for r in data[:3]] == [r[0] for r in a["rows"][:3]]


def test_single_figure_report_names_the_measure(client):
    a = ask(client, "Give me a report of RTPS PLF in March 2025")
    assert a["report_id"]
    wb, _ = download(client, a["report_id"])
    assert "Data" not in wb.sheetnames            # one figure: complete on the Report sheet
    rows = [r for r in wb["Report"].iter_rows(values_only=True)]
    fig = next(r for r in rows if r[2] == 64.95)
    assert "load factor" in str(fig[1]).lower() and fig[3] == "%"


@pytest.mark.parametrize("q", ["What was RTPS PLF in March 2025?", "Monthly KPCL generation trend in FY 2024-25",
                               "Top 5 stations by generation in FY 2024-25", "How much did YTPS generate in total during FY 2023-24?"])
def test_every_data_answer_gets_the_button(client, q):
    assert ask(client, q).get("report_id")


@pytest.mark.parametrize("q", ["What is an MIS report?", "History of KPCL", "What is KPCL?", "hello"])
def test_no_button_without_data(client, q):
    assert not ask(client, q).get("report_id")


def test_route_rules():
    assert answer_report.wants_report("which stations need attention first?", "report:attention", True, ["station"])
    assert answer_report.wants_report("RTPS PLF March 2025", "metric:plf", True, ["value"], [(64.95,)])
    assert not answer_report.wants_report("YCCPP generation FY 2016-17", "metric:gross_generation", True, ["value"], [(None,)])
    assert not answer_report.wants_report("report on KPCL history", "knowledge", True, [])
    assert not answer_report.wants_report("report", "report:x", False, ["a"])


def test_time_series_total_adds_only_what_adds_up():
    t = answer_report._time_series_total(["year_month", "gross_gen_mu", "plf_pct", "closing_stock_mt"],
                                          [("2024-04", 1.5, 50.0, 100.0), ("2024-05", 2.25, 60.0, 90.0)])
    assert t == ("Total", 3.75, None, None)     # PLF is not averaged, stock is not added across months
    assert answer_report._time_series_total(["station", "gross_gen_mu"], [("RTPS", 1.0), ("BTPS", 2.0)]) is None


def test_unknown_expired_and_malformed_ids_are_404(client, monkeypatch):
    for rid in ("nope", "A" * 24, "../../etc/passwd", "x" * 80):
        r = client.get(f"/api/answer-report/{rid}")
        assert r.status_code == 404
    rid = answer_report.keep("q report", "**a**", "report:x", ["station"], [("RTPS",)], False, None, {})
    assert answer_report.get(rid) is not None
    monkeypatch.setattr(answer_report, "KEEP_SECONDS", -1)
    assert answer_report.get(rid) is None


# ------------------------------------------------------------------ breakdowns: the answer, its Excel and the warehouse agree
@pytest.mark.parametrize("q,fy,station,n,first", [
    ("Weekly generation trend of RTPS in FY 2023-24", "2023-24", "RTPS", 53, "W1"),
    ("RTPS gross generation month-wise FY 2024-25", "2024-25", "RTPS", 12, "APR-2024"),
    ("Daily trend of KPCL generation in FY 2024-25", "2024-25", None, 365, "2024-04-01"),
    ("Quarterly generation of RTPS in FY 2024-25", "2024-25", "RTPS", 4, "Q1 (Apr–Jun)"),
    ("Station-wise generation in FY 2023-24", "2023-24", None, 33, "RTPS"),
    ("Unit wise generation of BTPS FY 2023-24", "2023-24", "BTPS", 3, "BTPS-U1"),
])
def test_breakdown_answer_excel_and_warehouse_agree(client, raw, q, fy, station, n, first):
    a = ask(client, q)
    assert a["report_id"] and len(a["rows"]) == n and a["rows"][0][0] == first, a["answer"]
    sql = "SELECT SUM(gross_gen_mu) FROM mart_fact_station_generation_fy WHERE fy = ?" + (" AND station_key = ?" if station else "")
    (want,) = raw.execute(sql, (fy, station) if station else (fy,)).fetchone()
    gen_col = a["columns"].index("gross_gen_mu")
    assert sum(r[gen_col] for r in a["rows"] if r[gen_col] is not None) == pytest.approx(want, abs=0.0005 * n + 1e-6)
    assert f"{want:,.3f} MU" in a["answer"]
    wb, _ = download(client, a["report_id"])
    data = [r for r in wb["Data"].iter_rows(min_row=6, values_only=True)]
    assert [r[gen_col] for r in data[:n]] == [r[gen_col] for r in a["rows"]]
    assert data[n][0] == "Total" and data[n][gen_col] == pytest.approx(want, abs=1e-3)
    checks = [r for r in wb["Report"].iter_rows(values_only=True) if r[2] in ("PASS", "CHECK")]
    assert checks and all(r[2] == "PASS" for r in checks)


def test_breakdown_leaves_other_measures_alone(client):
    a = ask(client, "Coal stock trend at RTPS in FY 2024-25")
    assert "generation" not in a["answer"].split("\n")[0].lower() and "totalled" not in a["answer"]


def test_plf_breakdown_leads_with_plf(client):
    a = ask(client, "Monthly PLF trend of YTPS FY 2023-24")
    assert a["answer"].startswith("**YTPS PLF in FY 2023-24: 41.88%") and "per month" not in a["answer"]   # PLF never averaged


def test_summary_words_get_figures_not_the_company_profile(client):
    for q in ("KPCL summary", "MIS report of SGS for FY 2024-25"):
        a = ask(client, q)
        assert "at a glance" in a["answer"] and a["report_id"]
