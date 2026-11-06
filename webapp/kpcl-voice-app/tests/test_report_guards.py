"""Guardrails on reports: no formulas, no workbook that fails its checks, draft marking, cut-short results, routing of
near-miss questions, the summary rule, default periods, units, data versions, escaping, inline scripts and charts."""
import io
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference

from app import answer_report, reports
from app import mis_report as mis
from app.main import app
from app.protection import CSP
from app.semantic_layer import extract_entities, norm
from app.xlsx_view import workbook_view
from conftest import run

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def keep(question="report", answer="**Report.**", route="report:x", columns=("station", "gross_gen_mu"),
         rows=(("RTPS", 1.0), ("BTPS", 2.0)), truncated=False, total=None, version=""):
    return answer_report.keep(question, answer, route, list(columns), list(rows), truncated, None, {}, total=total, version=version)


def sheets_xml(content: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(content))
    return "".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/sheet"))


# ------------------------------------------------------------------ 1. formula injection
@pytest.mark.parametrize("evil", ['=HYPERLINK("http://evil.example","Click")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(1,2)"])
def test_no_formula_ever_reaches_a_workbook(client, evil):
    rid = keep(question=f"{evil} report", columns=("label", "gross_gen_mu"), rows=((evil, 1.0), ("B", 2.0)))
    r = client.get(f"/api/answer-report/{rid}")
    assert r.status_code == 200
    xml = sheets_xml(r.content)
    assert "<f>" not in xml and "<f " not in xml                      # no cell is a formula
    wb = load_workbook(io.BytesIO(r.content))
    cells = [c for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith(evil[:4])]
    assert cells and all(c.data_type == "s" and c.quotePrefix for c in cells)


def test_chat_csv_download_escapes_formulas():
    js = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert "const safe = v =>" in js and "safe(v).replace" in js and "/^[=+\\-@\\t\\r]/" in js


# ------------------------------------------------------------------ 2. failing checks are never handed out
def test_failing_total_check_blocks_download_and_view(client):
    rid = keep(columns=("month", "gross_gen_mu"), rows=(("2024-04", 1.0), ("2024-05", 2.0)), total=("Total", 5.0))
    for path in (f"/api/answer-report/{rid}", f"/api/answer-report/{rid}/view"):
        r = client.get(path)
        assert r.status_code == 409 and r.json()["detail"] == "under_review" and "under review" in r.json()["message"]


def test_headline_that_disagrees_with_the_data_is_withheld(client):
    rid = keep(answer="**KPCL generated 999.000 MU in FY 2024-25.**", columns=("month", "gross_gen_mu"),
               rows=(("2024-04", 1.0), ("2024-05", 2.0)))
    assert client.get(f"/api/answer-report/{rid}").status_code == 409
    ok = keep(answer="**KPCL generated 3.000 MU in FY 2024-25.**", columns=("month", "gross_gen_mu"), rows=(("2024-04", 1.0), ("2024-05", 2.0)))
    assert client.get(f"/api/answer-report/{ok}").status_code == 200


def test_mis_report_that_fails_a_check_is_withheld(client, monkeypatch):
    real = mis._check
    monkeypatch.setattr(mis, "_check", lambda label, got, want, n: {**real(label, got, want, n), "ok": False})
    r = client.get("/api/mis-report", params={"fy": "2024-25", "station": "RTPS"})
    assert r.status_code == 409 and r.json()["detail"] == "under_review"


def test_real_reports_pass_their_checks(client):
    a = client.post("/api/ask_once", json={"question": "give me a report of rtps for financial year 2024-25"}).json()
    assert client.get(f"/api/answer-report/{a['report_id']}").status_code == 200


# ------------------------------------------------------------------ 3. draft marking
@pytest.mark.parametrize("path", ["/api/mis-report?fy=2023-24&grain=all", "ANSWER"])
def test_every_sheet_and_the_file_are_marked_draft(client, path):
    if path == "ANSWER":
        path = f"/api/answer-report/{keep()}"
    wb = load_workbook(io.BytesIO(client.get(path).content))
    for ws in wb.worksheets:
        assert ws.oddFooter.center.text == mis.DRAFT_NOTE, ws.title
    assert wb.properties.title.startswith("DRAFT") and wb.properties.subject == mis.DRAFT_NOTE
    assert "not for official use" in wb.properties.keywords


# ------------------------------------------------------------------ 4. cut-short results
def test_cut_short_result_is_flagged_on_the_report_sheet(client):
    wb = load_workbook(io.BytesIO(client.get(f"/api/answer-report/{keep(truncated=True)}").content))
    top = [c for row in wb["Report"].iter_rows(max_row=20, values_only=True) for c in row if isinstance(c, str)]
    assert any(t.startswith("INCOMPLETE RESULT") for t in top)


# ------------------------------------------------------------------ 5. near-miss questions never land in the breakdown
NEAR_MISS = [
    "Coal stock trend at RTPS in FY 2024-25", "Monthly coal consumption of RTPS FY 2024-25", "What was the RTPS turbine vibration trend in FY 2024-25?",
    "Monthly aux consumption of RTPS FY 2024-25", "Weekly trips at RTPS in FY 2023-24", "Station-wise heat rate in FY 2023-24",
    "Monthly outages at BTPS FY 2023-24", "Daily reservoir level of Linganamakki in March 2025", "Monthly back-down losses of RTPS FY 2024-25",
    "Top 5 stations by generation in FY 2024-25", "Which month in FY 2023-24 did SGS generate the least?", "Best month for RTPS generation in FY 2023-24",
    "Compare RTPS and BTPS monthly generation in FY 2024-25", "Monthly generation of RTPS in calendar year 2023", "Unit-wise generation of RTPS-U1 in FY 2024-25",
    "List all KPCL stations", "Show all RTPS units as a table", "KPCL's share of Karnataka's consumption over the years",
    "Compare thermal and hydro generation over the last five years.", "Show me the trend of forced outages over the ten years.",
    "Average monthly generation of RTPS in FY 2024-25", "Monthly PAF of RTPS FY 2024-25",
]
MUST_BREAK_DOWN = [
    "Monthly KPCL generation trend in FY 2024-25", "Weekly generation trend of RTPS in FY 2023-24", "Daily generation trend of RTPS in March 2025",
    "Station-wise generation in FY 2023-24", "Unit wise generation of BTPS FY 2023-24", "Yearly generation of KPCL", "Quarterly generation of RTPS in FY 2024-25",
    "Monthly PLF trend of YTPS FY 2023-24", "KPCL generation trend",
]


def _route(db, q):
    rep = run(reports.match_and_run(q, extract_entities(q), db, 20))
    return rep.key if rep else None


@pytest.mark.parametrize("q", NEAR_MISS)
def test_near_misses_never_become_a_generation_breakdown(db, q):
    assert not (_route(db, q) or "").startswith("generation_breakdown"), q


@pytest.mark.parametrize("q", MUST_BREAK_DOWN)
def test_breakdown_phrasings_still_work(db, q):
    key = _route(db, q)
    assert key and (key.startswith("generation_breakdown") or q.startswith("Monthly KPCL")), (q, key)


# ------------------------------------------------------------------ 6. the answer covers the measure asked about
@pytest.mark.parametrize("q", ["RTPS heat rate summary for FY 2023-24", "Give me an overview of RTPS GCV in FY 2023-24",
                               "Monthly PAF of RTPS FY 2024-25"])
def test_report_that_misses_the_measure_asked_is_not_used(db, q):
    rep = run(reports.match_and_run(q, extract_entities(q), db, 20))
    assert rep is None or reports.covers_asked_measures(norm(q), rep), (q, rep and rep.key)


def test_measure_check_keeps_matching_reports(db):
    q = "Monthly PLF trend of YTPS FY 2023-24"
    rep = run(reports.match_and_run(q, extract_entities(q), db, 20))
    assert rep and reports.covers_asked_measures(norm(q), rep)


# ------------------------------------------------------------------ 7. the summary rule leaves the organisation to the profile
@pytest.mark.parametrize("q", ["Summary of KPCL's CSR work", "Performance of KPCL's board", "Report on KPCL's history of dams",
                               "Give me an overview of KPCL's subsidiaries", "Summary of KPCL's upcoming projects"])
def test_questions_about_the_organisation_are_not_a_data_summary(db, q):
    assert reports.PROFILE_ASK.search(q) and _route(db, q) != "fy_summary"


@pytest.mark.parametrize("q", ["KPCL summary", "RTPS overview for FY 2023-24", "MIS report of SGS for FY 2024-25",
                               "Give me a summary of KPCL's generation performance for FY 2023-24."])
def test_figure_summaries_still_work(db, q):
    assert _route(db, q) == "fy_summary"


# ------------------------------------------------------------------ 8. the period used is stated when none was named
def test_default_period_is_stated_in_answer_and_excel(client):
    a = client.post("/api/ask_once", json={"question": "Which stations need attention first?"}).json()
    assert reports.DEFAULT_PERIOD_NOTE in a["answer"]
    wb = load_workbook(io.BytesIO(client.get(f"/api/answer-report/{a['report_id']}").content))
    rows = {r[1]: r[2] for r in wb["Report"].iter_rows(values_only=True) if r[1]}
    assert "FY 2024-25" in rows["Period used"]
    b = client.post("/api/ask_once", json={"question": "Performance report of all thermal stations in FY 2023-24"}).json()
    assert reports.DEFAULT_PERIOD_NOTE not in b["answer"]


def test_default_period_is_stated_in_the_mis_summary(client):
    a = client.post("/api/ask_once", json={"question": "KPCL summary"}).json()
    wb = load_workbook(io.BytesIO(client.get(f"/api/answer-report/{a['report_id']}").content))
    assert any(r[1] == "Period used" for r in wb["Summary"].iter_rows(values_only=True))


# ------------------------------------------------------------------ 9. units
def test_other_units_are_labelled(client):
    rid = keep(question="RTPS monthly generation FY 2024-25 in TWh", columns=("month", "gross_gen_mu"),
               rows=(("2024-04", 1.0), ("2024-05", 2.0)))
    wb = load_workbook(io.BytesIO(client.get(f"/api/answer-report/{rid}").content))
    rows = {r[1]: r[2] for r in wb["Report"].iter_rows(values_only=True) if r[1]}
    assert "MU" in rows["Units"] and "TWh" in rows["Units"]


# ------------------------------------------------------------------ 10. data version
def test_kept_reports_expire_when_the_warehouse_changes(client, db):
    now = mis.data_version(db)
    rid = keep(version=now)
    assert answer_report.get(rid, now) is not None
    assert answer_report.get(rid, "someother") is None
    wb = load_workbook(io.BytesIO(client.get("/api/mis-report", params={"fy": "2024-25"}).content))
    assert any(isinstance(c, str) and f"data version {now}" in c for r in wb["Summary"].iter_rows(values_only=True) for c in r)


# ------------------------------------------------------------------ 11. the viewer never builds pages from raw text
def test_viewer_escapes_cells_sheet_names_and_chart_titles():
    wb = Workbook()
    ws = wb.active
    ws.title = "<img src=x onerror=1>"
    ws["A1"], ws["A2"], ws["B2"] = "<script>alert(1)</script>", "<b>x</b>", 5
    ch = BarChart()
    ch.add_data(Reference(ws, min_col=2, min_row=2, max_row=2))
    ch.title = "</figcaption><script>alert(2)</script>"
    ws.add_chart(ch, "D2")
    v = workbook_view(mis.finalize(wb, []))
    html_ = v["sheets"][0]["html"]
    assert "<script" not in html_ and "<b>x" not in html_ and "&lt;script&gt;" in html_
    js = (STATIC / "report-viewer.js").read_text(encoding="utf-8")
    # every text from the server that the viewer places into markup goes through esc(): names, titles, footers, messages
    for needle in ("${esc(s.name)}", "${esc(c.title)}", "${esc(s.footer)}", "esc(text)"):
        assert needle in js, needle
    assert not re.search(r"\$\{(s\.name|c\.title|s\.footer|v\.name)\}", js)


# ------------------------------------------------------------------ 12. no inline scripts
def test_pages_have_no_inline_scripts_and_the_policy_forbids_them(client):
    assert "'unsafe-inline'" not in CSP.split("script-src")[1].split(";")[0]
    for page in ("/", "/chat", "/insights"):
        r = client.get(page)
        assert r.status_code == 200 and "content-security-policy" in {k.lower() for k in r.headers}
        assert not re.search(r"<script>(?!</script>)", r.text), page
        assert not re.search(r"\son(click|load|error|submit|change|input|key\w+|mouse\w+)\s*=", r.text), page
        for src in re.findall(r'<script src="(/static/[^"]+)"', r.text):
            assert client.get(src).status_code == 200, src


# ------------------------------------------------------------------ 13. charts are in the downloaded file itself
@pytest.mark.parametrize("params", [{"fy": "2023-24", "station": "RTPS", "grain": "all"}, {"fy": "2024-25", "grain": "weekly"}])
def test_every_chart_in_the_preview_is_a_native_chart_in_the_download(client, params):
    view = client.get("/api/mis-report/view", params=params).json()
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/mis-report", params=params).content))
    charts = [n for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)]
    assert len(charts) == sum(len(s["charts"]) for s in view["sheets"]) > 0
    for n in charts:                                                   # each carries its series, read from the workbook's own cells
        xml = z.read(n).decode()
        assert re.search(r"<(c:)?ser>", xml) and re.search(r"<(c:)?f>'[^']+'!\$[A-Z]+\$\d+", xml), n
