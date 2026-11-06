"""View report: the on-screen preview shows the same workbook as the download, sheet for sheet and figure for figure."""
import html
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import answer_report
from app.main import app
from app.xlsx_view import format_value


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("v,fmt,want", [
    (8671.845, "#,##0.00", "8,671.85"),                  # Excel rounds the number as written, not its binary double
    (57.4, "0.00", "57.40"), (6533495.01, "#,##0", "6,533,495"), (0.4399, "0.0%", "44.0%"), (366, '0" days"', "366 days"),
    (-0.5, '+0.0"%";-0.0"%";0.0"%"', "-0.5%"), (1.2, '+0.0"%";-0.0"%";0.0"%"', "+1.2%"),
    (0, '+0.00" pts";-0.00" pts";0.00" pts"', "0.00 pts"), (-2.24, '+0.00" pts";-0.00" pts";0.00" pts"', "-2.24 pts"),
    (-1234.5, "#,##0.00", "-1,234.50"), (12, "General", "12"), ("RTPS", "General", "RTPS"), (None, "0.00", ""),
])
def test_number_formats_match_excel(v, fmt, want):
    assert format_value(v, fmt) == want


def _cells_shown(sheet_html: str) -> set[str]:
    import re
    return {html.unescape(m) for m in re.findall(r"<td[^>]*>(.*?)</td>", sheet_html)}


@pytest.mark.parametrize("params", [{"fy": "2023-24", "station": "RTPS", "grain": "weekly"}, {"fy": "2024-25", "grain": "monthly"}])
def test_mis_view_is_the_downloaded_workbook(client, params):
    v = client.get("/api/mis-report/view", params=params)
    d = client.get("/api/mis-report", params=params)
    assert v.status_code == d.status_code == 200
    view, wb = v.json(), load_workbook(io.BytesIO(d.content))
    assert [s["name"] for s in view["sheets"]] == wb.sheetnames
    assert view["name"] in d.headers["content-disposition"]
    for s in view["sheets"]:
        ws, shown = wb[s["name"]], _cells_shown(s["html"])
        for row in ws.iter_rows():
            for c in row:
                # the time of generation is the only thing allowed to differ (the two were built a moment apart)
                if c.value is None or ws.cell(c.row, 2).value == "Generated on" or str(c.value).startswith("Generated "):
                    continue
                assert format_value(c.value, c.number_format) in shown, (s["name"], c.coordinate, c.value)
    weekly = next((s for s in view["sheets"] if s["name"] == "Generation - Weekly"), None)
    if weekly:
        ch = weekly["charts"][0]
        assert len(ch["labels"]) == 53 and ch["datasets"][0]["color"] == "#072577"


def test_answer_view_and_expiry(client, monkeypatch):
    a = client.post("/api/ask_once", json={"question": "Quarterly generation of RTPS in FY 2024-25"}).json()
    v = client.get(f"/api/answer-report/{a['report_id']}/view").json()
    assert [s["name"] for s in v["sheets"]] == ["Report", "Data", "Chart", "Notes"]
    data = _cells_shown(v["sheets"][1]["html"])
    assert {"Q1 (Apr–Jun)", "Total", "8,310.351"} <= data
    assert client.get("/api/answer-report/" + "A" * 24 + "/view").status_code == 404


def test_cell_text_is_escaped(client):
    rid = answer_report.keep("<script>alert(1)</script> report", "**<img src=x onerror=alert(1)>**", "report:x",
                             ["station", "gross_gen_mu"], [("<b>RTPS</b>", 1.0), ("BTPS", 2.0)], False, None, {})
    v = client.get(f"/api/answer-report/{rid}/view").json()
    joined = "".join(s["html"] for s in v["sheets"])
    assert "<script>" not in joined and "<img src=x" not in joined and "<b>RTPS" not in joined
    assert "&lt;script&gt;" in joined
