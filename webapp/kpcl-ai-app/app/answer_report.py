"""
Excel report for an AI-mode answer: GET /api/answer-report/{report_id}

Every chat answer that was computed from the data (a reviewed report, a breakdown, a verified query, a single figure
or a model-written query that passed review) is kept for a while under a random id, and the page shows a
"Download report" button under it. Answers not taken from the data (definitions, general knowledge, refusals, "no
figure" replies) get none. The workbook is built only from
what the pipeline itself produced and kept on the server, never from anything the page sends back:

  * a summary of KPCL or one station for an FY (the dashboard's own highlights) -> the full MIS workbook for that
    selection (app/mis_report.py), identical to the dashboard's MIS report, with the question noted on the Summary;
  * anything else -> an answer workbook in the same house style:
      Report   letterhead, the question, the findings exactly as answered, the key figures, and (when the question
               names one FY and at most one station) the dashboard's KPIs for that scope, for context
      Data     every row the answer was computed from, with units and number formats
      Chart    the answer's chart, when it had one, as a native Excel chart
      Notes    definitions

Kept answers live in memory for KEEP_SECONDS (a download after that asks the user to ask again); at most KEEP_MAX are
kept, oldest dropped first. Nothing is written to disk.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment

from app import answer as writer
from app import dashboard_data as dd
from app import metric_lookup
from app import mis_report as mis
from app.db import ReadOnlyDB
from app.semantic_layer import data_as_of

KEEP_SECONDS = 3 * 3600
KEEP_MAX = 500
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# answers that were computed from the data (not refusals, glossary, knowledge-base or identity answers)
_DATA_ROUTES = ("report:", "verified:", "metric:", "llm")


@dataclass
class Kept:
    question: str
    answer: str
    route: str
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    chart: dict | None
    entities: dict = field(default_factory=dict)
    total: tuple | None = None
    total_checked: bool = False   # True when the total came from the period's own figures, not from adding the rows
    version: str = ""             # warehouse version the answer was computed from (mis_report.data_version)
    created: float = field(default_factory=time.time)


_store: OrderedDict[str, Kept] = OrderedDict()
_lock = threading.Lock()


def wants_report(question: str, route: str, answerable: bool, columns: list[str], rows: list | None = None) -> bool:
    """True for every answer computed from the data that has at least one figure in it."""
    if not answerable or not columns or not route.startswith(_DATA_ROUTES):
        return False
    return rows is None or any(v is not None for r in rows for v in r)


def keep(question: str, answer: str, route: str, columns: list[str], rows: list, truncated: bool, chart: dict | None,
         entities: dict, total: tuple | None = None, version: str = "") -> str:
    """Keep one answer and return its id."""
    rid = secrets.token_urlsafe(18)
    rows = [tuple(r) for r in rows]
    independent = total is not None     # a report's total comes from the year's own figures, so it can be checked
    if total is None and not truncated:
        total = _time_series_total(list(columns), rows)
    item = Kept(question, answer, route, list(columns), rows, truncated, chart, dict(entities or {}),
                tuple(total) if total else None, independent, version)
    with _lock:
        _store[rid] = item
        now = time.time()
        while _store and (len(_store) > KEEP_MAX or now - next(iter(_store.values())).created > KEEP_SECONDS):
            _store.popitem(last=False)
    return rid


def get(rid: str, version: str | None = None) -> Kept | None:
    """The kept answer, unless it has expired or the warehouse has been rebuilt since (a different data version)."""
    if not ID_RE.match(rid or ""):
        return None
    with _lock:
        item = _store.get(rid)
    if item is None or time.time() - item.created > KEEP_SECONDS:
        return None
    if version is not None and item.version and item.version != version:
        with _lock:
            _store.pop(rid, None)
        return None
    return item


# ------------------------------------------------------------------------------------------------ which workbook
def _one_fy(e: dict) -> str | None:
    """The FY the question names, when it names exactly one period and that is an FY."""
    ps = e.get("periods", [])
    return ps[0]["value"] if len(ps) == 1 and ps[0]["kind"] == "fy" else None


def _grain(question: str) -> str:
    q = question.lower()
    if re.search(r"\b(complete|full|detailed|comprehensive|everything|all (the )?(sheets|views|periods))\b", q):
        return "all"
    if re.search(r"\b(weekly|week[- ]?wise|weeks?)\b", q):
        return "weekly"
    if re.search(r"\b(daily|day[- ]?wise|days?)\b", q):
        return "daily"
    return "monthly"


def _mis_selection(k: Kept) -> tuple[str | None, str | None] | None:
    """(fy, station) when the answer was the dashboard summary of KPCL or one station for an FY, else None."""
    if k.route != "report:fy_summary":
        return None
    e = k.entities
    stations = e.get("stations", [])
    if len(stations) > 1 or e.get("categories") or e.get("units"):
        return None
    ps = e.get("periods", [])
    if ps and not _one_fy(e):
        return None
    return (_one_fy(e) or "2024-25"), (stations[0] if stations else None)


def _context_selection(k: Kept) -> tuple[str, str | None] | None:
    """(fy, station) for the dashboard KPIs shown as context: one FY named, at most one station, nothing narrower."""
    e = k.entities
    fy = _one_fy(e)
    stations = e.get("stations", [])
    if not fy or len(stations) > 1 or e.get("categories") or e.get("plant_type") or e.get("units") or e.get("reservoirs"):
        return None
    return fy, (stations[0] if stations else None)


# ------------------------------------------------------------------------------------------------ answer workbook
def _findings(answer: str) -> tuple[str, list[str]]:
    """(headline, lines) from the chat answer: the bold first line, then each paragraph or bullet as written."""
    text = answer.strip()
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    headline = ""
    if lines and lines[0].startswith("**"):
        headline = mis._plain(lines.pop(0)).rstrip(":").strip()
    out = []
    for x in lines:
        x = mis._plain(re.sub(r"^[•\-\*]\s*", "", x)).strip()
        if x and not x.lower().startswith("ask about any"):   # chat guidance, not a finding
            out.append(x)
    return headline, out


def _col_fmt(col: str, value=None) -> str:
    unit = writer.col_unit(col)
    places = writer._PRECISION.get(unit, 2) if unit and unit not in ("count",) else None
    if unit == "count":
        return "#,##0"
    if places is None:
        f = writer.to_float(value)
        return "#,##0" if f is not None and float(f).is_integer() else "#,##0.00"
    return "#,##0" if places == 0 else "#,##0." + "0" * places


def _measure(k: Kept) -> tuple[str, str, int] | None:
    """(label, unit, decimals) of a single-figure answer ("metric:plf" with one column named 'value')."""
    if not k.route.startswith("metric:") or k.columns != ["value"]:
        return None
    try:
        m = metric_lookup._metric(k.route.split(":", 1)[1])
    except Exception:  # noqa: BLE001 - an unknown key just keeps the plain column name
        return None
    if not m:
        return None
    label = m.get("label") or "Value"
    return label[:1].upper() + label[1:], m.get("unit") or "", int(m.get("precision", 2))


def _headers(columns: list[str]) -> list[str]:
    t = writer.result_table(columns, [tuple([None] * len(columns))] * 2)
    return t["headers"] if t else [writer.header(c) for c in columns]


def _kinds(columns: list[str], rows: list[tuple]) -> list[str]:
    out = []
    for i, c in enumerate(columns):
        numeric = any(writer.to_float(r[i]) is not None for r in rows[:50]) and not c.lower().endswith(("_key", "_code"))
        out.append("number" if numeric else "text")
    return out


def _time_series_total(columns: list[str], rows: list[tuple]) -> tuple | None:
    """For a time series (every label a distinct period), a total row of the columns that can be summed over time
    (MU, tonnes, hours, counts); ratios and positions such as PLF or stock are left blank, never averaged or added."""
    if len(rows) < 2 or len(columns) < 2:
        return None
    labels = [r[0] for r in rows]
    if not all(writer.time_key(v) for v in labels) or len(set(labels)) != len(labels):
        return None
    out: list = ["Total"]
    for i, c in enumerate(columns[1:], 1):
        vals = [writer.to_float(r[i]) for r in rows]
        ok = (writer.summable_over_time(c) and writer.col_unit(c) in ("MU", "tonnes", "hours", "count", "kL")
              and all(v is not None for v in vals) and not isinstance(rows[0][i], str))
        out.append(round(sum(vals), 6) if ok else None)
    return tuple(out) if any(v is not None for v in out[1:]) else None


_HEADLINE_MU = re.compile(r"\b(?:generated|totalled|totalling|totaling)\s+\**\s*(-?[\d,]+(?:\.\d+)?)\s*MU\b")


def _shown_equals(shown: str, actual: float) -> bool:
    """A figure as written in the answer (to however many decimals it shows) equals the actual value."""
    places = len(shown.split(".")[1]) if "." in shown else 0
    return abs(float(shown.replace(",", "")) - actual) <= 0.5 * 10 ** -places + 1e-9


def _headline_checks(k: Kept) -> list[dict]:
    """The generation figure the answer states ("generated 37,602.179 MU", "totalled ... MU") equals the Data sheet total."""
    if not k.total or len(k.rows) < 2:
        return []
    m = _HEADLINE_MU.search(k.answer)
    if not m:
        return []
    mu = [i for i, c in enumerate(k.columns) if writer.col_unit(c) == "MU" and isinstance(k.total[i], (int, float))]
    if not mu:
        return []
    i = k.columns.index("gross_gen_mu") if "gross_gen_mu" in k.columns and k.columns.index("gross_gen_mu") in mu else mu[0]
    shown, total = m.group(1), float(k.total[i])
    return [{"label": "The figure in the answer matches the Data sheet total", "ok": _shown_equals(shown, total),
             "detail": f"answer {shown} MU against the Data sheet total {total:,.3f} MU"}]


def _mis_headline_check(k: Kept, wb: Workbook) -> dict | None:
    """For a summary answer: its "generated X MU" equals the MIS workbook's gross generation."""
    m = re.search(r"generated \*\*(-?[\d,]+(?:\.\d+)?) MU", k.answer)
    kpi = next((r[3] for r in wb["Summary"].iter_rows(values_only=True) if len(r) > 3 and r[1] == "Gross generation"), None)
    if not m or not isinstance(kpi, (int, float)):
        return None
    return {"label": "The figure in the answer matches the MIS workbook", "ok": _shown_equals(m.group(1), float(kpi)),
            "detail": f"answer {m.group(1)} MU against the workbook's {kpi:,.3f} MU"}


def _notes(k: Kept) -> list[tuple[str, str]]:
    """Label / text rows for the top of the report: the period used when none was named, and the units."""
    out = []
    if "No period was named" in k.answer:
        out.append(("Period used", "No period was named in the question, so the latest complete financial year, FY 2024-25, is used."))
    ru = writer.requested_unit(k.question)
    if ru:
        base = {"MU": "MU (million units = GWh)", "MW": "MW", "tonnes": "tonnes", "MCFT": "MCFT", "feet": "feet", "metres": "metres"}.get(ru[0], ru[0])
        out.append(("Units", f"Figures in this workbook are in {base}, the unit the data is kept in; the answer also shows them in {ru[1]}."))
    return out


def _checks(k: Kept) -> list[dict]:
    """The rows must add up to the total row, for every additive column the total row fills in."""
    if not k.total or not k.total_checked or len(k.rows) < 2:
        return []
    out = []
    for i, c in enumerate(k.columns):
        want = k.total[i]
        if not isinstance(want, (int, float)) or isinstance(want, bool) or not writer.summable_over_time(c) or writer.col_unit(c) not in ("MU", "MW", "hours", "count", "tonnes"):
            continue
        vals = [writer.to_float(r[i]) for r in k.rows if writer.to_float(r[i]) is not None]
        got = sum(vals)
        tol = 0.0005 * max(len(vals), 1) + 1e-9
        out.append({"label": f"{_headers(k.columns)[i]}: the rows add up to the total", "ok": abs(got - want) <= tol,
                    "detail": f"{got:,.3f} against {want:,.3f} (difference {got - want:+.3f}; allowed for rounding ±{tol:.3f})"})
    return out


def _sheet_report(wb: Workbook, k: Kept, d: dict | None, generated: dt.datetime, checks: list[dict] | None = None) -> None:
    ws = wb.active
    ws.title = "Report"
    mis._gov_header(ws, "Report")
    headline, lines = _findings(k.answer)
    e = k.entities
    scope = []
    if e.get("stations"):
        scope.append(", ".join(e["stations"]))
    if e.get("units"):
        scope.append(", ".join(e["units"]))
    if e.get("categories"):
        scope.append(", ".join(mis._CAT_LABEL.get(c, c.title()) for c in e["categories"]))
    if e.get("periods"):
        scope.append(", ".join(p.get("label") or p.get("value", "") for p in e["periods"]))
    meta = [("Question asked", f"“{k.question.strip()}”")]
    if scope:
        meta.append(("Scope", " · ".join(scope)))
    meta += _notes(k)
    meta += [("Data available to", mis._dmy(data_as_of()) + (f"  ·  data version {k.version}" if k.version else "")),
             ("Generated on", generated.strftime("%d-%b-%Y %H:%M"))]
    r = mis._title_and_meta(ws, headline or "Report", meta)
    if k.truncated:
        r += 2
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        c = ws.cell(r, 2, f"INCOMPLETE RESULT: only the first {len(k.rows):,} rows were returned, the most one answer may hold. "
                          "No totals or checks are computed for a cut-short result. Ask a narrower question (one station, "
                          "one financial year or one month) to get the full data.")
        c.font, c.fill = mis._f(size=11, bold=True, color=mis.BAD), mis._fill(mis.BAD_SOFT)
        c.alignment = Alignment(wrap_text=True, vertical="center", indent=1)
        for col in range(2, 7):
            ws.cell(r, col).border = mis.Border(left=mis.Side(style="medium", color=mis.BAD), right=mis.Side(style="medium", color=mis.BAD),
                                                top=mis.Side(style="medium", color=mis.BAD), bottom=mis.Side(style="medium", color=mis.BAD))
            ws.cell(r, col).fill = mis._fill(mis.BAD_SOFT)
        ws.row_dimensions[r].height = 50

    if lines:
        r += 2
        mis._section(ws, r, "Findings")
        for x in lines:
            r += 1
            ws.cell(r, 1, "●").font = mis._f(size=9, bold=True, color=mis.GOLD)
            ws.cell(r, 1).alignment = Alignment(horizontal="right", vertical="top")
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
            c = ws.cell(r, 2, x)
            c.font, c.alignment = mis._f(size=10, color=mis.INK), Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 15 * max(1, -(-len(x) // 92))

    # key figures: a single-row result (one figure or one entity) reads best as a two-column list
    if len(k.rows) == 1:
        r += 2
        mis._section(ws, r, "Key figures")
        r += 1
        mis._header(ws, r, ["Measure", "Value", "Unit"], numeric_from=3, start_col=2)
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
        m = _measure(k)
        for c, v, h in zip(k.columns, k.rows[0], _headers(k.columns)):
            r += 1
            num = writer.to_float(v) is not None and not c.lower().endswith(("_key", "_code"))
            unit = m[1] if m else (writer.col_unit(c) or "")
            fmt = ("#,##0" if m[2] == 0 else "#,##0." + "0" * m[2]) if m else _col_fmt(c, v)
            name = m[0] if m else re.sub(r"\s*(\([^)]*\)|%)$", "", h) if unit else h
            mis._cell(ws, r, 2, name, bold=True, fill=mis._band(r))
            mis._cell(ws, r, 3, writer.to_float(v) if num else writer.pretty_label(v) if v is not None else "no data",
                      fmt if num else None, fill=mis._band(r), align="right")
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
            mis._cell(ws, r, 4, "" if unit == "count" else unit, color=mis.MUTED, align="center", fill=mis._band(r))
            ws.cell(r, 5).border = mis._BOX
    elif k.rows:
        r += 2
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        c = ws.cell(r, 2, f"The {len(k.rows):,} rows behind these findings are on the Data sheet"
                          + (" with a chart on the Chart sheet." if k.chart else "."))
        c.font = mis._f(size=10, italic=True, color=mis.MUTED)

    if d is not None:
        r = mis._kpi_block(ws, r + 1, d)
        ws.merge_cells(start_row=r + 1, start_column=2, end_row=r + 1, end_column=6)
        ws.cell(r + 1, 2, "Context from the dashboard for the same FY and scope; the same figures as the dashboard and its MIS report.").font = \
            mis._f(size=9, italic=True, color=mis.MUTED)
        r += 1
    if checks:
        r += 2
        mis._section(ws, r, "Integrity checks")
        for ck in checks:
            r += 1
            mis._cell(ws, r, 2, ck["label"], bold=True, wrap=True)
            mis._cell(ws, r, 3, "PASS" if ck["ok"] else "CHECK", align="center", bold=True, color=mis.GOOD if ck["ok"] else mis.BAD,
                      fill=mis.GOOD_SOFT if ck["ok"] else mis.BAD_SOFT)
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=6)
            mis._cell(ws, r, 4, ck["detail"], color=mis.MUTED, size=9, wrap=True)
            for col in (5, 6):
                ws.cell(r, col).border = mis._BOX
            ws.row_dimensions[r].height = 28
    r += 2
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    c = ws.cell(r, 2, "Figures come from read-only queries on the Generation Management System data, the same that produced the answer in AI mode. "
                      "See the Notes sheet for definitions. Proof-of-concept data: only the RTPS and SGS figures pinned to K-eRP reports are real.")
    c.font, c.alignment = mis._f(size=9, italic=True, color=mis.MUTED), Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 40
    ws.sheet_properties.tabColor = mis.NAVY
    mis._print_setup(ws, None, landscape=False)


def _sheet_data(wb: Workbook, k: Kept) -> None:
    if len(k.rows) < 2:   # a single row is shown in full under "Key figures" on the Report sheet
        return
    ws = wb.create_sheet("Data")
    cols, rows = k.columns, k.rows
    kinds = _kinds(cols, rows)
    hdr = mis._banner(ws, "Data behind the answer", f"{len(rows):,} row{'s' if len(rows) != 1 else ''} · every figure the answer was computed from",
                      len(cols))
    first_num = next((i + 1 for i, kd in enumerate(kinds) if kd == "number"), None)
    mis._header(ws, hdr, _headers(cols), numeric_from=first_num)
    for i, row in enumerate(rows):
        r = hdr + 1 + i
        for j, (c, v) in enumerate(zip(cols, row), 1):
            if kinds[j - 1] == "number":
                mis._cell(ws, r, j, writer.to_float(v), _col_fmt(c, v), fill=mis._band(r))
            else:
                keep_raw = c.lower().endswith(("_key", "_code"))
                mis._cell(ws, r, j, (v if keep_raw else writer.pretty_label(v)) if v is not None else None, bold=j == 1,
                          fill=mis._band(r))
    last = hdr + len(rows)
    if k.total:
        tr = last + 1
        for j, (c, v) in enumerate(zip(cols, k.total), 1):
            num = isinstance(v, (int, float)) and not isinstance(v, bool)
            mis._cell(ws, tr, j, v, _col_fmt(c, v) if num else None)
        mis._total_style(ws, tr, len(cols))
        note = ("Total row: the period's own figures from the dashboard; ratios (PLF, achievement, change) are recomputed from summed "
                "totals, never averaged. Comparisons of single weeks or days with last year are not totalled." if k.total_checked else
                "Total row: the sum of the periods above, for quantities that add up over time (MU, tonnes, hours, counts). "
                "Percentages, ratios and stock or level positions are not added or averaged, so they are left blank.")
        ws.cell(tr + 2, 1, note).font = mis._f(size=9, italic=True, color=mis.MUTED)
    for j, c in enumerate(cols, 1):
        if kinds[j - 1] != "number":
            continue
        L = ws.cell(hdr + 1, j).column_letter
        if "achievement" in c:
            mis._colour_achievement(ws, f"{L}{hdr + 1}:{L}{last + (1 if k.total else 0)}", f"{L}{hdr + 1}")
        elif re.search(r"yoy|growth|change", c):
            mis._colour_change(ws, f"{L}{hdr + 1}:{L}{last + (1 if k.total else 0)}", f"{L}{hdr + 1}")
    ru = writer.requested_unit(k.question)
    if ru:
        ws.cell(last + (5 if k.total else 3), 1, f"Figures are in the units the data is kept in ({ru[0]}); the answer also shows them in {ru[1]}.").font = \
            mis._f(size=9, italic=True, color=mis.MUTED)
    if k.truncated:
        ws.cell(last + (4 if k.total else 2), 1, f"Only the first {len(rows):,} rows were returned; the answer notes that the result was cut short.").font = \
            mis._f(size=9, italic=True, color=mis.BAD)
    widths = []
    for j, (h, kd) in enumerate(zip(_headers(cols), kinds)):
        longest = max([len(str(writer.pretty_label(r[j]) if kd == "text" and r[j] is not None else writer.fmt_value(cols[j], r[j])))
                       for r in rows[:200]] + [0])
        widths.append(max(10, min(46, max(longest + 2, len(h) * 0.9))))
    ws.auto_filter.ref = f"A{hdr}:{ws.cell(hdr, len(cols)).column_letter}{last}"
    mis._widths(ws, widths)
    ws.freeze_panes = f"B{hdr + 1}" if len(cols) > 2 else f"A{hdr + 1}"
    mis._print_setup(ws, hdr, landscape=len(cols) > 5)
    ws.sheet_properties.tabColor = mis.GOLD


def _sheet_chart(wb: Workbook, k: Kept) -> None:
    c = k.chart
    if not c or len(c.get("values") or []) < 2:
        return
    ws = wb.create_sheet("Chart")
    title = (c.get("series_label") or "Value") + (f" ({c['unit']})" if c.get("unit") and c["unit"] not in (c.get("series_label") or "") else "")
    hdr = mis._banner(ws, title, "The chart shown with the answer, drawn from the same rows", 3)
    mis._header(ws, hdr, ["Label", title], numeric_from=2)
    fmt = "#,##0.000" if c.get("unit") == "MU" else "#,##0.00"
    for i, (lab, v) in enumerate(zip(c["labels"], c["values"])):
        r = hdr + 1 + i
        mis._cell(ws, r, 1, lab, bold=True, fill=mis._band(r))
        mis._cell(ws, r, 2, v, fmt, fill=mis._band(r))
    last = hdr + len(c["values"])
    ch = LineChart() if c.get("type") == "line" else BarChart()
    if isinstance(ch, BarChart):
        ch.type = "col"
    ch.add_data(Reference(ws, min_col=2, min_row=hdr, max_row=last), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
    mis._series_style(ch.series[0], mis.NAVY, line=isinstance(ch, LineChart))
    mis._style_chart(ch, title, c.get("unit") or "", width=max(18, min(34, 1.1 * len(c["values"]) + 10)))
    ch.legend = None
    ws.add_chart(ch, f"D{hdr}")
    mis._finish_sheet(ws, hdr, 2, [28, 22], freeze_col="A")
    ws.sheet_properties.tabColor = mis.GOLD


_NOTE_KEYS = ("Financial year", "Gross generation (MU)", "PLF (%)", "CEA target achievement (%)", "Auxiliary consumption (%)",
              "As on", "Change", "Data status")


def _sheet_notes(wb: Workbook, generated: dt.datetime) -> None:
    ws = wb.create_sheet("Notes")
    hdr = mis._banner(ws, "Notes and definitions", "How the figures in this workbook are defined", 2)
    mis._header(ws, hdr, ["Item", "Definition"])
    notes = [("Purpose", "The answer given in AI mode, with the rows it was computed from, in a form that can be filed, printed or "
                         "filtered. Nothing here is typed in by hand: every figure is written from the same query result as the answer.")]
    notes += [n for n in mis._NOTES if n[0] in _NOTE_KEYS]
    for i, (a, b) in enumerate(notes):
        r = hdr + 1 + i
        mis._cell(ws, r, 1, a, bold=True, fill=mis._band(r), wrap=True)
        mis._cell(ws, r, 2, b, fill=mis._band(r), wrap=True)
        ws.row_dimensions[r].height = 15 * max(1, -(-len(b) // 105)) + 4
    r = hdr + len(notes) + 2
    ws.cell(r, 1, f"Generated {generated.strftime('%d-%b-%Y %H:%M')} from KPCL Generation Management System data (data to {mis._dmy(data_as_of())}).").font = \
        mis._f(size=9, italic=True, color=mis.MUTED)
    mis._finish_sheet(ws, hdr, 2, [30, 112], freeze_col="A")
    ws.sheet_properties.tabColor = mis.MUTED


def _make(k: Kept, d: dict | None, generated: dt.datetime) -> Workbook:
    wb = Workbook()
    checks = _checks(k) + _headline_checks(k)
    _sheet_report(wb, k, d, generated, checks)
    _sheet_data(wb, k)
    _sheet_chart(wb, k)
    _sheet_notes(wb, generated)
    wb.properties.title = f"KPCL report — {k.question.strip()[:120]}"
    wb.properties.creator = "KPCL Generation Management System"
    return mis.finalize(wb, checks)


def _file_name(k: Kept, generated: dt.datetime) -> str:
    e = k.entities
    parts = ["KPCL_Report"]
    if e.get("stations") and len(e["stations"]) <= 3:
        parts.append("-".join(e["stations"]))
    elif e.get("categories") and len(e["categories"]) == 1:
        parts.append(mis._CAT_LABEL.get(e["categories"][0], e["categories"][0]).split(" ")[0])
    ps = e.get("periods", [])
    if len(ps) == 1:
        parts.append(("FY" + ps[0]["value"]) if ps[0]["kind"] == "fy" else (ps[0].get("label") or ps[0]["value"]))
    parts.append(generated.strftime("%Y%m%d-%H%M"))
    return mis._slug("_".join(parts)) + ".xlsx"


async def build_answer_report(db: ReadOnlyDB, k: Kept, timeout_seconds: float, as_workbook: bool = False) -> tuple[bytes | Workbook, str]:
    """(xlsx bytes, file name) for a kept answer, or (the openpyxl Workbook, file name) for the on-screen viewer.
    A workbook that fails any check raises mis.ReportUnderReview and is never handed out."""
    sel = _mis_selection(k)
    if sel is not None:
        fy, station = sel
        try:
            wb, name = await mis.build_mis_report(db, fy, station, _grain(k.question), timeout_seconds, asked=k.question.strip(),
                                                  as_workbook=True, notes=_notes(k))
        except dd.DashboardNotFound:
            wb = None   # fall back to the answer workbook
        if wb is not None:
            ck = _mis_headline_check(k, wb)
            if ck is not None:
                wb.kpcl_checks.append(ck)
                if not ck["ok"]:
                    raise mis.ReportUnderReview([ck])
            return (wb if as_workbook else await asyncio.to_thread(mis.workbook_bytes, wb)), name
    generated = dt.datetime.now()
    d = None
    ctx = _context_selection(k)
    if ctx is not None:
        try:
            d = await dd.build_dashboard(db, ctx[0], ctx[1], timeout_seconds)
        except dd.DashboardNotFound:
            d = None
    wb = await asyncio.to_thread(_make, k, d, generated)
    failed = mis.failed_checks(wb)
    if failed:
        raise mis.ReportUnderReview(failed)
    return (wb if as_workbook else await asyncio.to_thread(mis.workbook_bytes, wb)), _file_name(k, generated)
