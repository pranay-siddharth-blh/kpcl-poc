"""
MIS report (Excel) for GET /api/mis-report?fy=2023-24&station=RTPS&grain=weekly

The workbook is a snapshot of exactly what the dashboard shows for the same selection. It does not run its own
version of the numbers: every figure comes from dashboard_data.build_dashboard() (the same fixed, reviewed, read-only
queries and the same formulas as the metric registry), plus two more fixed queries in the same style for the station
table and the station-by-month matrix. Ratios are recomputed from summed totals, never averaged.

Sheets (only those that have data for the selection):
  Summary            scope, period, KPIs against the previous FY, key observations, integrity checks
  Generation - ...   the period the user was viewing (Monthly / Weekly / Daily), or all three for grain=all
  Stations | Units   all-KPCL: every station with category subtotals; one station: its units
  Station x Month    all-KPCL only: monthly generation of every station
  Ten-year trend     FY-by-FY series behind the dashboard's trend chart
  Generation loss, Units out, Coal stock, Reservoirs   the dashboard's panels ("as on" the last day in the data)
  Notes              definitions, formulas, data status

Integrity checks are computed from the numbers being written and printed on the Summary sheet: the months, weeks,
days and stations must add up to the year's total, within the rounding of the values written (half of the last digit
per row). A check that fails is shown as CHECK in red, never hidden.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.drawing.image import Image as XlImage
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from app import dashboard_data as dd
from app.db import ReadOnlyDB

GRAINS = ("monthly", "weekly", "daily", "all")
DRAFT_NOTE = "Proof of concept – not for official use"
# Text that a spreadsheet would read as a formula (or, in a CSV, as the start of one) when a user edits the cell
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class ReportUnderReview(Exception):
    """A workbook failed one of its own integrity checks, so it is not handed out (-> 409, "under review")."""

    def __init__(self, failed: list[dict]):
        super().__init__("; ".join(f"{c['label']}: {c['detail']}" for c in failed))
        self.failed = failed
_STATIC = Path(__file__).resolve().parent / "static"

# palette shared with dashboard.css
NAVY, NAVY_DEEP, NAVY_SOFT = "072577", "04164A", "EEF1F8"
GOLD, GOLD_SOFT, GOLD_LINE = "B8892B", "F7F1E3", "E4D3AB"
GOOD, GOOD_SOFT, WARN, WARN_SOFT, BAD, BAD_SOFT = "1F9D57", "E5F5EC", "C98A00", "FDF3DC", "C8372D", "FCEBEA"
INK, MUTED, BORDER = "1A2233", "5F6B7D", "CFD5E2"

_FONT = "Calibri"
_thin = Side(style="thin", color=BORDER)
_BOX = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
_FMT_MU, _FMT_PCT, _FMT_INT = "#,##0.00", "0.00", "#,##0"
_FMT_CHG_PCT, _FMT_CHG_PTS = '+0.0"%";-0.0"%";0.0"%"', '+0.00" pts";-0.00" pts";0.00" pts"'
_CAT_LABEL = {"THERMAL-COAL": "Thermal (coal)", "MAJOR HYDRO": "Major hydro", "MINOR HYDRO": "Minor hydro", "GAS": "Gas",
              "SOLAR": "Solar", "WIND": "Wind", "RES": "Waste to energy"}
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# station-by-month matrix (all-KPCL only); the FY comes from a bound parameter
_MATRIX_SQL = """
    SELECT station_key, year_month, month_label, SUM(gross_gen_mu) AS gen
    FROM mart_fact_station_generation_monthly WHERE fy = ?
    GROUP BY station_key, year_month, month_label ORDER BY year_month
"""


# ------------------------------------------------------------------------------------------------ small helpers
def _dmy(d: str | None) -> str:
    return "—" if not d else f"{int(d[8:10]):02d}-{_MON[int(d[5:7]) - 1]}-{d[:4]}"


def _month_name(label: str | None) -> str:
    """'APR-2024' -> 'Apr 2024'"""
    return "" if not label else label[:1] + label[1:3].lower() + " " + label[4:]


def _fy_span(fy: str, as_of: str | None, partial: bool) -> str:
    start = dt.date(int(fy[:4]), 4, 1)
    end = dt.date(int(fy[:4]) + 1, 3, 31)
    if partial and as_of:
        end = dt.date.fromisoformat(as_of[:10])
    return f"{start.day:02d}-{_MON[start.month - 1]}-{start.year} to {end.day:02d}-{_MON[end.month - 1]}-{end.year}"


def _plain(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def _pct_change(cur, prev):
    return dd._pct_change(cur, prev)


def _pts(cur, prev):
    return None if cur is None or prev is None else round(cur - prev, 2)


def _f(**kw) -> Font:
    return Font(name=_FONT, **kw)


def _fill(hex_: str) -> PatternFill:
    return PatternFill("solid", start_color=hex_, end_color=hex_)


def _widths(ws, widths: list[float]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _banner(ws, title: str, subtitle: str, ncols: int) -> int:
    """Navy title band over the first three rows. Returns the row to start the content on."""
    ncols = max(ncols, 4)
    for r, (text, font, h) in enumerate([
            (title, _f(size=15, bold=True, color="FFFFFF"), 26),
            (subtitle, _f(size=10, color="E9CF8F"), 18)], 1):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        c = ws.cell(r, 1, text)
        c.font, c.alignment = font, Alignment(vertical="center", indent=1)
        ws.row_dimensions[r].height = h
        for col in range(1, ncols + 1):
            ws.cell(r, col).fill = _fill(NAVY)
    for col in range(1, ncols + 1):
        ws.cell(3, col).fill = _fill(GOLD)
    ws.row_dimensions[3].height = 3
    ws.sheet_view.showGridLines = False
    return 5


def _header(ws, row: int, headers: list[str], numeric_from: int | None = None, start_col: int = 1) -> None:
    for i, h in enumerate(headers, start_col):
        c = ws.cell(row, i, h)
        c.font, c.fill, c.border = _f(bold=True, color="FFFFFF", size=10), _fill(NAVY), _BOX
        c.alignment = Alignment(horizontal="left" if i == start_col or (numeric_from and i < numeric_from) else "center",
                                vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 32


def _cell(ws, row: int, col: int, value, fmt: str | None = None, *, bold=False, fill: str | None = None,
          align: str | None = None, color: str | None = None, wrap=False, size=10):
    if isinstance(value, float):
        value = round(value, 6)   # drops float noise such as 8310.351000000002
    c = ws.cell(row, col, value)
    c.font = _f(bold=bold, color=color or INK, size=size)
    c.border = _BOX
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = _fill(fill)
    c.alignment = Alignment(horizontal=align or ("right" if isinstance(value, (int, float)) else "left"),
                            vertical="center", wrap_text=wrap)
    return c


def _band(row: int) -> str | None:
    return NAVY_SOFT if row % 2 == 0 else None


def _total_style(ws, row: int, ncols: int) -> None:
    for col in range(1, ncols + 1):
        c = ws.cell(row, col)
        c.font = _f(bold=True, color=NAVY_DEEP, size=10)
        c.fill = _fill(GOLD_SOFT)
        c.border = Border(left=_thin, right=_thin, top=Side(style="medium", color=GOLD), bottom=Side(style="medium", color=GOLD))


def _colour_change(ws, rng: str, first_cell: str) -> None:
    """Green for rises, red for falls in a change column; blank cells stay unstyled."""
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f"AND(ISNUMBER({first_cell}),{first_cell}>0)"], font=Font(color=GOOD, bold=True)))
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f"AND(ISNUMBER({first_cell}),{first_cell}<0)"], font=Font(color=BAD, bold=True)))


def _colour_achievement(ws, rng: str, first_cell: str) -> None:
    """The dashboard's thresholds: at least 100 green, 95 to 100 amber, below 95 red."""
    for cond, bg, fg in ((f"AND(ISNUMBER({first_cell}),{first_cell}>=100)", GOOD_SOFT, GOOD),
                         (f"AND(ISNUMBER({first_cell}),{first_cell}>=95,{first_cell}<100)", WARN_SOFT, WARN),
                         (f"AND(ISNUMBER({first_cell}),{first_cell}<95)", BAD_SOFT, BAD)):
        ws.conditional_formatting.add(rng, FormulaRule(formula=[cond], fill=_fill(bg), font=Font(color=fg, bold=True)))


def _print_setup(ws, header_row: int | None, landscape=True) -> None:
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = ws.page_margins.right = 0.4
    if header_row:
        ws.print_title_rows = f"{header_row}:{header_row}"
    ws.oddFooter.left.text = "KPCL · Generation Management System · MIS report"
    ws.oddFooter.center.text = DRAFT_NOTE
    ws.oddFooter.right.text = "Page &P of &N"


def _check(label: str, got: float, want: float | None, n_rows: int) -> dict:
    """Rows that were each rounded to 3 decimals may differ from the unrounded total by at most 0.0005 per row."""
    if want is None:
        return {"label": label, "ok": None, "detail": "not applicable"}
    tol = 0.0005 * max(n_rows, 1) + 1e-9
    diff = got - want
    return {"label": label, "ok": abs(diff) <= tol,
            "detail": f"{got:,.3f} against {want:,.3f} (difference {diff:+.3f} MU; allowed for rounding ±{tol:.3f})"}


# ------------------------------------------------------------------------------------------------ sheets
def _gov_header(ws, subtitle: str) -> None:
    """The navy letterhead (emblem, Government of Karnataka, KPCL) across columns A to G of a portrait sheet."""
    _widths(ws, [4, 38, 12, 20, 20, 22, 4])
    for r in range(1, 4):
        for col in range(1, 8):
            ws.cell(r, col).fill = _fill(NAVY)
    ws.row_dimensions[1].height, ws.row_dimensions[2].height, ws.row_dimensions[3].height = 22, 24, 16
    ws.merge_cells("B1:F1"); ws.merge_cells("B2:F2"); ws.merge_cells("B3:F3")
    ws["B1"], ws["B2"], ws["B3"] = "GOVERNMENT OF KARNATAKA", "Karnataka Power Corporation Limited", f"Generation Management System  ·  {subtitle}"
    ws["B1"].font, ws["B2"].font, ws["B3"].font = _f(size=9, bold=True, color="E9CF8F"), _f(size=16, bold=True, color="FFFFFF"), _f(size=10, color="FFFFFF")
    for a in ("B1", "B2", "B3"):
        ws[a].alignment = Alignment(vertical="center")
    for col in range(1, 8):
        ws.cell(4, col).fill = _fill(GOLD)
    ws.row_dimensions[4].height = 3
    ws.sheet_view.showGridLines = False
    try:
        img = XlImage(str(_STATIC / "karnataka-emblem.png"))
        img.width = img.height = 56
        ws.add_image(img, "A1")
        ws.column_dimensions["A"].width = 9
    except Exception:  # noqa: BLE001 - the logo is decoration; the report stands without it
        pass


def _title_and_meta(ws, title: str, meta: list[tuple[str, str]]) -> int:
    """Big title on row 6 and label / value rows under it. Returns the last row written."""
    ws.merge_cells("B6:F6")
    ws["B6"] = title
    ws["B6"].font = _f(size=18, bold=True, color=NAVY)
    ws["B6"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[6].height = 30 if len(title) <= 48 else 50
    r = 7
    for lab, val in meta:
        ws.cell(r, 2, lab).font = _f(size=10, bold=True, color=MUTED)
        ws.cell(r, 2).alignment = Alignment(vertical="top")
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
        c = ws.cell(r, 3, val)
        c.font, c.alignment = _f(size=10, color=INK), Alignment(wrap_text=True, vertical="top")
        if len(val) > 70:
            ws.row_dimensions[r].height = 15 * -(-len(val) // 70)
        r += 1
    return r - 1


def _section(ws, r: int, text: str) -> None:
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    ws.cell(r, 2, text).font = _f(size=12, bold=True, color=NAVY)


def _kpi_block(ws, r: int, d: dict) -> int:
    """'Key performance indicators' for the dashboard selection d, starting after row r. Returns the last row written."""
    st, fy, pfy, partial = d["scope"], d["fy"], d["prev_fy"], d["is_partial"]
    k, cur, prev = d["kpis"], d["kpis"]["cur"], d["kpis"]["prev"]
    r += 2
    _section(ws, r, "Key performance indicators" + (f" · {st['station']}" if st else "") + f" · FY {fy}")
    r += 1
    _header(ws, r, ["Indicator", "Unit", f"FY {fy}" + (" (part)" if partial else ""), f"FY {pfy}" if pfy else "Previous FY", "Change"],
            numeric_from=4, start_col=2)
    first_kpi = r + 1
    rows = [("Gross generation", "MU", "gen", _FMT_MU, "pct", True), ("Plant load factor (PLF)", "%", "plf", _FMT_PCT, "pts", True)]
    if not st:
        rows += [("Thermal PLF", "%", "thermal_plf", _FMT_PCT, "pts", True), ("Hydro generation", "MU", "hydro_gen", _FMT_MU, "pct", True)]
    rows += [("CEA generation target", "MU", "target", _FMT_MU, "pct", None), ("CEA target achievement", "%", "achievement", _FMT_PCT, "pts", True),
             ("Auxiliary consumption (share of generation)", "%", "aux", _FMT_PCT, "pts", False),
             ("Forced outage", "hours", "forced_hrs", "#,##0.0", "pct", False), ("Unit trips", "nos", "trips", _FMT_INT, "pct", False),
             ("Coal consumed", "tonnes", "coal_mt", _FMT_INT, "pct", None)]
    for lab, unit, key, fmt, kind, good_up in rows:
        cv, pv = cur.get(key), prev.get(key)
        if cv is None and pv is None:
            continue
        r += 1
        chg = None if partial else (_pct_change(cv, pv) if kind == "pct" else _pts(cv, pv))
        _cell(ws, r, 2, lab, bold=True)
        _cell(ws, r, 3, unit, align="center", color=MUTED)
        _cell(ws, r, 4, cv, fmt)
        _cell(ws, r, 5, pv, fmt)
        c = _cell(ws, r, 6, chg if chg is not None else ("n/a (part year)" if partial and pv is not None else None),
                  _FMT_CHG_PCT if kind == "pct" else _FMT_CHG_PTS, align="right", bold=True)
        if chg is not None and good_up is not None and abs(chg) >= 0.005:
            c.font = _f(bold=True, color=GOOD if (chg > 0) == good_up else BAD)
        elif partial:
            c.font = _f(color=MUTED, italic=True)
    if not st and k.get("share") is not None:
        r += 1
        for col, (v, fmt) in enumerate([("Share of Karnataka's consumption", None), ("%", None), (k["share"], _FMT_PCT), (k.get("share_prev"), _FMT_PCT)], 2):
            _cell(ws, r, col, v, fmt, bold=col == 2, align="center" if col == 3 else None, color=MUTED if col == 3 else None)
        chg = None if partial else _pts(k["share"], k.get("share_prev"))
        c = _cell(ws, r, 6, chg if chg is not None else ("n/a (part year)" if partial else None), _FMT_CHG_PTS, bold=True, align="right")
        if chg is not None and abs(chg) >= 0.005:
            c.font = _f(bold=True, color=GOOD if chg > 0 else BAD)
        if k.get("peak_mw"):
            r += 1
            _cell(ws, r, 2, "State peak demand", bold=True); _cell(ws, r, 3, "MW", align="center", color=MUTED)
            _cell(ws, r, 4, k["peak_mw"], _FMT_INT); _cell(ws, r, 5, None); _cell(ws, r, 6, None)
    for rr in range(first_kpi, r + 1):
        if rr % 2 == 1:
            for col in range(2, 7):
                if ws.cell(rr, col).fill.fgColor.rgb in (None, "00000000"):
                    ws.cell(rr, col).fill = _fill(NAVY_SOFT)
    return r


def _position_block(ws, r: int, d: dict) -> int:
    """Units out, coal and reservoirs as on the last day in the data, starting after row r. Returns the last row written."""
    st, k = d["scope"], d["kpis"]
    r += 2
    _section(ws, r, f"Position as on {_dmy(d['as_of'])}")
    pos = [("Units out of service", f"{k['units_out']} of {k['units_total']}" + (f"  ({dd._fmt(k['mw_out'], 0)} MW unavailable)" if k["units_out"] else ""))]
    if d["coal"]:
        below = [c for c in d["coal"] if c["below"]]
        low = min(d["coal"], key=lambda c: c["days"] or 0)
        pos.append(("Thermal stations below normative coal stock", f"{len(below)} of {len(d['coal'])}; lowest {low['station']} at {dd._fmt(low['days'], 1)} days (norm {low['norm']})"))
    if d["reservoirs"] and (not st or st["type"] == "HYDRO"):
        lo = min(d["reservoirs"], key=lambda x: x["pct"] or 0)
        pos.append(("Lowest reservoir", f"{lo['name'].title()} at {dd._fmt(lo['pct'], 1)}% of capacity"))
    for lab, val in pos:
        r += 1
        _cell(ws, r, 2, lab, bold=True)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
        _cell(ws, r, 3, val)
        for col in range(4, 7):
            ws.cell(r, col).border = _BOX
    return r


def _sheet_summary(wb: Workbook, d: dict, grain: str, checks: list[dict], generated: dt.datetime, asked: str | None = None,
                   notes: list[tuple[str, str]] | None = None, version: str = "") -> None:
    ws = wb.active
    ws.title = "Summary"
    st, fy, pfy, partial = d["scope"], d["fy"], d["prev_fy"], d["is_partial"]
    k = d["kpis"]
    _gov_header(ws, "MIS Report")
    meta = ([("Requested in AI mode", f"\u201c{asked}\u201d")] if asked else []) + list(notes or []) + [
            ("Report period", f"FY {fy}" + (" (part year, to the last date in the data)" if partial else "") + f"  ·  {_fy_span(fy, d['as_of'], partial)}"),
            ("Compared with", f"FY {pfy}" if pfy else "—"),
            ("Data available to", _dmy(d["as_of"]) + (f"  ·  data version {version}" if version else "")),
            ("Scope", (f"{_CAT_LABEL.get(st['category'], st['category'])} · {dd._fmt(st['capacity_mw'], 1)} MW · {st['unit_count']} units" if st
                       else f"{k['station_count']} stations · {dd._fmt(k['installed_mw'], 0)} MW installed")),
            ("Generation detail included", {"monthly": "Monthly", "weekly": "Weekly (Monday to Sunday)", "daily": "Daily",
                                            "all": "Monthly, weekly and daily"}[grain]),
            ("Generated on", generated.strftime("%d-%b-%Y %H:%M"))]
    r = _title_and_meta(ws, f"{st['station']} — {st['name']}" if st else "KPCL — all stations", meta)
    r = _kpi_block(ws, r, d)
    r = _position_block(ws, r, d)

    # ---- observations
    if d["highlights"]:
        r += 2
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        ws.cell(r, 2, "Key observations").font = _f(size=12, bold=True, color=NAVY)
        mark = {"good": ("▲", GOOD), "bad": ("▼", BAD), "info": ("●", NAVY)}
        for h in d["highlights"]:
            r += 1
            sym, colr = mark.get(h["tone"], mark["info"])
            ws.cell(r, 1, sym).font = _f(size=10, bold=True, color=colr)
            ws.cell(r, 1).alignment = Alignment(horizontal="right", vertical="top")
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
            c = ws.cell(r, 2, _plain(h["text"]))
            c.font, c.alignment = _f(size=10, color=INK), Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 15 * max(1, -(-len(c.value) // 92))

    # ---- integrity checks
    if checks:
        r += 2
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        ws.cell(r, 2, "Integrity checks (figures in this workbook add up to the year's total)").font = _f(size=12, bold=True, color=NAVY)
        for ck in checks:
            r += 1
            ok = ck["ok"]
            _cell(ws, r, 2, ck["label"], bold=True)
            _cell(ws, r, 3, "n/a" if ok is None else "PASS" if ok else "CHECK", align="center", bold=True,
                  color=MUTED if ok is None else GOOD if ok else BAD, fill=None if ok is None else GOOD_SOFT if ok else BAD_SOFT)
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=6)
            _cell(ws, r, 4, ck["detail"], color=MUTED, size=9, wrap=True)
            for col in (5, 6):
                ws.cell(r, col).border = _BOX
            ws.row_dimensions[r].height = 26
    r += 2
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    c = ws.cell(r, 2, "Figures come from fixed, read-only queries on the Generation Management System data and match the dashboard for the same selection. "
                      "See the Notes sheet for definitions. Proof-of-concept data: only the RTPS and SGS figures pinned to K-eRP reports are real.")
    c.font, c.alignment = _f(size=9, italic=True, color=MUTED), Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 40
    ws.sheet_properties.tabColor = NAVY
    _print_setup(ws, None, landscape=False)


def _finish_sheet(ws, header_row: int, ncols: int, widths: list[float], freeze_col: str = "B", print_table_only: bool = False) -> None:
    _widths(ws, widths)
    ws.freeze_panes = f"{freeze_col}{header_row + 1}"
    ws.auto_filter.ref = None
    _print_setup(ws, header_row)
    if ws.max_row <= 50:   # a short sheet (with its chart underneath) prints on one page instead of splitting the chart
        ws.page_setup.fitToHeight = 1
    if print_table_only:   # the chart sits to the right on screen; keep it from shrinking the printed table
        ws.print_area = f"A1:{get_column_letter(ncols)}{ws.max_row}"


def _style_chart(ch, title: str, y_title: str, width=24, height=9.5) -> None:
    ch.title, ch.width, ch.height = title, width, height
    ch.title.overlay = False
    ch.y_axis.title = y_title
    ch.x_axis.delete = ch.y_axis.delete = False   # newer Excel hides the axes of openpyxl charts unless told not to
    ch.legend.position = "b"
    ch.legend.overlay = False


def _series_style(s, colour: str, *, line=False, dash: str | None = None, width=22000, smooth=False) -> None:
    if line:
        s.graphicalProperties.line.solidFill = colour
        s.graphicalProperties.line.width = width
        if dash:
            s.graphicalProperties.line.dashStyle = dash
        s.marker.symbol = "none"
        s.smooth = smooth
    else:
        s.graphicalProperties.solidFill = colour
        s.graphicalProperties.line.solidFill = colour


def _sheet_monthly(wb: Workbook, d: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Generation - Monthly")
    fy, pfy, cur = d["fy"], d["prev_fy"], d["kpis"]["cur"]
    who = d["scope"]["station"] if d["scope"] else "KPCL"
    hdr = _banner(ws, f"Monthly generation · {who} · FY {fy}", f"Gross generation in MU against FY {pfy} and the CEA target; PLF from summed capacity",
                  8)
    heads = ["Month", "Gross generation (MU)", f"FY {pfy} (MU)" if pfy else "Previous FY (MU)", "Change on previous FY", "CEA target (MU)",
             "CEA target achievement (%)", "PLF (%)"]
    _header(ws, hdr, heads, numeric_from=2)
    rows = d["monthly"]
    for i, m in enumerate(rows):
        r = hdr + 1 + i
        _cell(ws, r, 1, _month_name(m["month"]), bold=True, fill=_band(r))
        _cell(ws, r, 2, m["gen"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 3, m["prev"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 4, _pct_change(m["gen"], m["prev"]), _FMT_CHG_PCT, fill=_band(r))
        _cell(ws, r, 5, m["target"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 6, dd._ratio(m["gen"], m["target"]) if m["target"] else None, _FMT_PCT, fill=_band(r))
        _cell(ws, r, 7, m["plf"], _FMT_PCT, fill=_band(r))
    last = hdr + len(rows)
    tr = last + 1
    prev_sum = round(sum(m["prev"] for m in rows if m["prev"] is not None), 3) if any(m["prev"] is not None for m in rows) else None
    _cell(ws, tr, 1, "Year to date" if d["is_partial"] else "FY total")
    _cell(ws, tr, 2, cur["gen"], _FMT_MU)
    _cell(ws, tr, 3, prev_sum, _FMT_MU)
    _cell(ws, tr, 4, _pct_change(cur["gen"], prev_sum), _FMT_CHG_PCT)
    _cell(ws, tr, 5, cur["target"], _FMT_MU)
    _cell(ws, tr, 6, cur["achievement"], _FMT_PCT)
    _cell(ws, tr, 7, cur["plf"], _FMT_PCT)
    _total_style(ws, tr, 7)
    ws.cell(tr + 2, 1, "The total row is the year's figure from the dashboard; PLF and achievement are recomputed from summed totals, not averaged. "
                       "Achievement in the total row counts only months that have a CEA target.").font = _f(size=9, italic=True, color=MUTED)
    _colour_change(ws, f"D{hdr + 1}:D{tr}", f"D{hdr + 1}")
    _colour_achievement(ws, f"F{hdr + 1}:F{tr}", f"F{hdr + 1}")
    if rows:
        bar = BarChart(); bar.type = "col"
        bar.add_data(Reference(ws, min_col=2, min_row=hdr, max_row=last), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
        _series_style(bar.series[0], NAVY)
        line = LineChart()
        for col, colour, dash in ((3, "E9B949", None), (5, BAD, "dash")):
            if any(ws.cell(hdr + 1 + i, col).value is not None for i in range(len(rows))):
                line.add_data(Reference(ws, min_col=col, min_row=hdr, max_row=last), titles_from_data=True)
                _series_style(line.series[-1], colour, line=True, dash=dash)
        _style_chart(bar, f"Monthly generation, FY {fy} (MU)", "MU", width=21, height=9)
        if line.series:
            bar += line
        ws.add_chart(bar, f"A{tr + 4}")
    _finish_sheet(ws, hdr, 7, [16, 18, 16, 16, 16, 20, 12])
    ws.sheet_properties.tabColor = GOLD
    gens = [m["gen"] for m in rows if m["gen"] is not None]
    checks.append(_check("Months add up to the year", sum(gens), cur["gen"], len(gens)))


def _sheet_weekly(wb: Workbook, d: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Generation - Weekly")
    fy, pfy, cur = d["fy"], d["prev_fy"], d["kpis"]["cur"]
    who = d["scope"]["station"] if d["scope"] else "KPCL"
    hdr = _banner(ws, f"Weekly generation · {who} · FY {fy}",
                  f"Calendar weeks, Monday to Sunday; each compared with the same week 364 days earlier (FY {pfy})", 10)
    heads = ["Week", "Month", "From", "To", "Days", "Gross generation (MU)", "Same week last year (MU)", "Change on last year", "PLF (%)", "Note"]
    _header(ws, hdr, heads, numeric_from=5)
    rows = d["weekly"]
    for i, w in enumerate(rows):
        r = hdr + 1 + i
        note = []
        if w["days"] < 7:
            note.append(f"part week ({w['days']} days)")
        if w["prev_days"] is not None and w["prev_days"] != w["days"]:
            note.append(f"last year's week has {w['prev_days']} days")
        _cell(ws, r, 1, f"W{w['week']}", bold=True, fill=_band(r))
        _cell(ws, r, 2, _month_name(w["month"]), fill=_band(r))
        _cell(ws, r, 3, _dmy(w["first"]), align="center", fill=_band(r))
        _cell(ws, r, 4, _dmy(w["last"]), align="center", fill=_band(r))
        _cell(ws, r, 5, w["days"], "0", align="center", fill=_band(r))
        _cell(ws, r, 6, w["gen"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 7, w["prev"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 8, _pct_change(w["gen"], w["prev"]), _FMT_CHG_PCT, fill=_band(r))
        _cell(ws, r, 9, w["plf"], _FMT_PCT, fill=_band(r))
        _cell(ws, r, 10, "; ".join(note), color=MUTED, size=9, fill=_band(r))
    last = hdr + len(rows)
    tr = last + 1
    _cell(ws, tr, 1, "Year to date" if d["is_partial"] else "FY total")
    for col in (2, 3, 4):
        _cell(ws, tr, col, None)
    _cell(ws, tr, 5, sum(w["days"] for w in rows), "0", align="center")
    _cell(ws, tr, 6, cur["gen"], _FMT_MU)
    _cell(ws, tr, 7, None)
    _cell(ws, tr, 8, None)
    _cell(ws, tr, 9, cur["plf"], _FMT_PCT)
    _cell(ws, tr, 10, None)
    _total_style(ws, tr, 10)
    ws.cell(tr + 2, 1, "The first and last weeks of a financial year are usually part weeks. CEA targets are set monthly, so no weekly target is shown. "
                       "Each week is compared with its own counterpart, so these comparisons do not add up to last year's total; the year-on-year change for the whole year is on the Summary sheet.").font = _f(size=9, italic=True, color=MUTED)
    _colour_change(ws, f"H{hdr + 1}:H{tr}", f"H{hdr + 1}")
    if rows:
        bar = BarChart(); bar.type = "col"
        bar.add_data(Reference(ws, min_col=6, min_row=hdr, max_row=last), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
        _series_style(bar.series[0], NAVY)
        if any(w["prev"] is not None for w in rows):
            line = LineChart()
            line.add_data(Reference(ws, min_col=7, min_row=hdr, max_row=last), titles_from_data=True)
            _series_style(line.series[0], "E9B949", line=True)
            bar += line
        _style_chart(bar, f"Weekly generation, FY {fy} (MU)", "MU", width=28)
        ws.add_chart(bar, f"L{hdr}")
    _finish_sheet(ws, hdr, 10, [8, 12, 13, 13, 7, 18, 20, 16, 10, 34], print_table_only=True)
    ws.sheet_properties.tabColor = GOLD
    gens = [w["gen"] for w in rows if w["gen"] is not None]
    checks.append(_check("Weeks add up to the year", sum(gens), cur["gen"], len(gens)))


def _sheet_daily(wb: Workbook, d: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Generation - Daily")
    fy, pfy, cur = d["fy"], d["prev_fy"], d["kpis"]["cur"]
    who = d["scope"]["station"] if d["scope"] else "KPCL"
    hdr = _banner(ws, f"Daily generation · {who} · FY {fy}", f"Each day compared with the same date in FY {pfy} (29 February has no partner)", 8)
    heads = ["Date", "Day", "Gross generation (MU)", "PLF (%)", "Same date last year", "Last year (MU)", "Change on last year"]
    _header(ws, hdr, heads, numeric_from=3)
    rows = d["daily"]
    for i, x in enumerate(rows):
        r = hdr + 1 + i
        _cell(ws, r, 1, _dmy(x["date"]), bold=True, align="center", fill=_band(r))
        _cell(ws, r, 2, x["day"], align="center", fill=_band(r))
        _cell(ws, r, 3, x["gen"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 4, x["plf"], _FMT_PCT, fill=_band(r))
        _cell(ws, r, 5, _dmy(x["prev_date"]) if x["prev_date"] else None, align="center", color=MUTED, fill=_band(r))
        _cell(ws, r, 6, x["prev"], _FMT_MU, fill=_band(r))
        _cell(ws, r, 7, _pct_change(x["gen"], x["prev"]), _FMT_CHG_PCT, fill=_band(r))
    last = hdr + len(rows)
    tr = last + 1
    _cell(ws, tr, 1, "Year to date" if d["is_partial"] else "FY total")
    _cell(ws, tr, 2, len(rows), '0" days"', align="center")
    _cell(ws, tr, 3, cur["gen"], _FMT_MU)
    _cell(ws, tr, 4, cur["plf"], _FMT_PCT)
    _cell(ws, tr, 5, None)
    _cell(ws, tr, 6, None)
    _cell(ws, tr, 7, None)
    _total_style(ws, tr, 7)
    ws.cell(tr + 2, 1, "Each day is compared with its own counterpart, so these comparisons do not add up to last year's total; "
                       "the year-on-year change for the whole year is on the Summary sheet.").font = _f(size=9, italic=True, color=MUTED)
    _colour_change(ws, f"G{hdr + 1}:G{tr}", f"G{hdr + 1}")
    if rows:
        ch = LineChart()
        ch.add_data(Reference(ws, min_col=3, min_row=hdr, max_row=last), titles_from_data=True)
        _series_style(ch.series[0], NAVY, line=True, width=15000)
        if any(x["prev"] is not None for x in rows):
            ch.add_data(Reference(ws, min_col=6, min_row=hdr, max_row=last), titles_from_data=True)
            _series_style(ch.series[1], "E9B949", line=True, dash="dash", width=12000)
        ch.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
        _style_chart(ch, f"Daily generation, FY {fy} (MU)", "MU", width=28)
        ch.x_axis.tickLblSkip = 30
        ws.add_chart(ch, f"I{hdr}")
    _finish_sheet(ws, hdr, 7, [14, 8, 20, 10, 18, 16, 18], print_table_only=True)
    ws.sheet_properties.tabColor = GOLD
    gens = [x["gen"] for x in rows if x["gen"] is not None]
    checks.append(_check("Days add up to the year", sum(gens), cur["gen"], len(gens)))


def _sheet_stations(wb: Workbook, d: dict, ex: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Stations")
    fy, pfy, cur = d["fy"], d["prev_fy"], d["kpis"]["cur"]
    hdr = _banner(ws, f"Station-wise performance · KPCL · FY {fy}",
                  f"Grouped by type of plant with subtotals; change is against FY {pfy}", 13)
    heads = ["Station", "Name", "Capacity (MW)", "Units", "Gross generation (MU)", f"FY {pfy} (MU)" if pfy else "Previous FY (MU)", "Change on previous FY",
             "PLF (%)", "CEA target (MU)", "CEA target achievement (%)", "Auxiliary consumption (%)", "Units out now", "Category"]
    _header(ws, hdr, heads, numeric_from=3)
    by_station = ex["stations"]
    prev_known = pfy is not None
    r = hdr

    def line(label, name, cap, units, gen, prev_gen, cap_mu, target, aux, out, cat, gen_t, total=False):
        """One row. Ratios come from the summed figures passed in; achievement counts only generation that has a target."""
        nonlocal r
        r += 1
        vals = [label, name, cap, units, None if gen is None else round(gen, 3), None if prev_gen is None else round(prev_gen, 3),
                _pct_change(gen, prev_gen), dd._ratio(gen, cap_mu), None if not target else round(target, 3),
                dd._ratio(gen_t, target) if target else None, dd._ratio(aux, gen), out, cat]
        fmts = [None, None, "#,##0.0", "0", _FMT_MU, _FMT_MU, _FMT_CHG_PCT, _FMT_PCT, _FMT_MU, _FMT_PCT, _FMT_PCT, "0", None]
        for col, (v, f) in enumerate(zip(vals, fmts), 1):
            _cell(ws, r, col, v, f, bold=col == 1, fill=None if total else _band(r), color=MUTED if col == 13 else None,
                  align="center" if col in (4, 12) else None)
        if total:
            _total_style(ws, r, 13)

    def new_sum():
        return {"cap": 0.0, "units": 0, "gen": 0.0, "prev": 0.0, "cap_mu": 0.0, "target": 0.0, "gen_t": 0.0, "aux": 0.0, "out": 0}

    categories = list(dict.fromkeys(s["category"] for s in d["stations"]))
    gen_t_of = {s["station"]: (by_station.get(s["station"], {}).get("gen") if by_station.get(s["station"], {}).get("target") else None)
                for s in d["stations"]}
    grand = new_sum()
    for cat in categories:
        sub = new_sum()
        members = [s for s in d["stations"] if s["category"] == cat]
        for s in members:
            x = by_station.get(s["station"], {})
            gen, prev_gen = x.get("gen"), (x.get("prev_gen") if prev_known else None)
            line(s["station"], s["name"], s["capacity_mw"], s["units"], gen, prev_gen, x.get("cap"), x.get("target"), x.get("aux"),
                 s["units_out"] or 0, _CAT_LABEL.get(cat, cat), gen_t_of[s["station"]])
            for key, v in (("cap", s["capacity_mw"]), ("units", s["units"]), ("gen", gen), ("prev", prev_gen), ("cap_mu", x.get("cap")),
                           ("target", x.get("target")), ("gen_t", gen_t_of[s["station"]]), ("aux", x.get("aux")), ("out", s["units_out"])):
                sub[key] += v or 0
        for key in sub:
            grand[key] += sub[key]
        has_gen = any(by_station.get(m["station"], {}).get("gen") is not None for m in members)
        has_prev = any(by_station.get(m["station"], {}).get("prev_gen") is not None for m in members)
        line(f"{_CAT_LABEL.get(cat, cat)} — total", f"{len(members)} stations", sub["cap"], sub["units"], sub["gen"] if has_gen else None,
             sub["prev"] if prev_known and has_prev else None, sub["cap_mu"], sub["target"], sub["aux"], sub["out"], "", sub["gen_t"], total=True)
    line("KPCL — all stations", f"{len(d['stations'])} stations", grand["cap"], grand["units"], grand["gen"],
         grand["prev"] if prev_known else None, grand["cap_mu"], grand["target"], grand["aux"], grand["out"], "", grand["gen_t"], total=True)
    for col in range(1, 14):
        ws.cell(r, col).fill = _fill(NAVY_SOFT)
    last = r
    ws.cell(last + 2, 1, "Units out now: units out of service on the last date in the data. Achievement counts only stations that have a CEA target. "
                         "Ratios are recomputed from summed totals.").font = _f(size=9, italic=True, color=MUTED)
    _colour_change(ws, f"G{hdr + 1}:G{last}", f"G{hdr + 1}")
    _colour_achievement(ws, f"J{hdr + 1}:J{last}", f"J{hdr + 1}")
    _finish_sheet(ws, hdr, 13, [22, 34, 12, 8, 18, 16, 16, 10, 16, 18, 18, 10, 16])
    ws.sheet_properties.tabColor = NAVY
    checks.append(_check("Stations add up to KPCL's total", round(grand["gen"], 3), cur["gen"], len(d["stations"])))


def _sheet_units(wb: Workbook, d: dict, ex: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Units")
    st, fy, cur = d["scope"], d["fy"], d["kpis"]["cur"]
    hdr = _banner(ws, f"Unit-wise performance · {st['station']} · FY {fy}", "PLF on capacity; PAF on declared capacity; status as on the last date in the data", 9)
    heads = ["Unit", "Capacity (MW)", "Gross generation (MU)", "PLF (%)", "PAF (%)", "Running hours", "Trips", "Status now"]
    _header(ws, hdr, heads, numeric_from=2)
    rows = d["units"]
    gens_full = {u["unit"]: None if ex["units"].get(u["unit"]) is None else round(ex["units"][u["unit"]], 3) for u in rows}
    for i, u in enumerate(rows):
        r = hdr + 1 + i
        out = u["status"] and u["status"] != "RUNNING"
        _cell(ws, r, 1, u["unit"], bold=True, fill=_band(r))
        _cell(ws, r, 2, u["capacity_mw"], "#,##0.0", fill=_band(r))
        _cell(ws, r, 3, gens_full[u["unit"]], _FMT_MU, fill=_band(r))
        _cell(ws, r, 4, u["plf"], _FMT_PCT, fill=_band(r))
        _cell(ws, r, 5, u["paf"], _FMT_PCT, fill=_band(r))
        _cell(ws, r, 6, u["running_hrs"], _FMT_INT, fill=_band(r))
        _cell(ws, r, 7, u["trips"], _FMT_INT, fill=_band(r))
        _cell(ws, r, 8, (u["status"] or "—").replace("_", " ").capitalize(), align="center", bold=bool(out), fill=_band(r),
              color=BAD if out else GOOD)
    last = hdr + len(rows)
    tr = last + 1
    _cell(ws, tr, 1, f"{st['station']} total")
    _cell(ws, tr, 2, sum(u["capacity_mw"] or 0 for u in rows), "#,##0.0")
    _cell(ws, tr, 3, cur["gen"], _FMT_MU)
    _cell(ws, tr, 4, cur["plf"], _FMT_PCT)
    _cell(ws, tr, 5, None)
    _cell(ws, tr, 6, sum(u["running_hrs"] or 0 for u in rows), _FMT_INT)
    _cell(ws, tr, 7, sum(u["trips"] or 0 for u in rows), _FMT_INT)
    _cell(ws, tr, 8, None)
    _total_style(ws, tr, 8)
    ws.cell(tr + 2, 1, "The total row's generation and PLF are the station's figures from the dashboard.").font = _f(size=9, italic=True, color=MUTED)
    if rows:
        ch = BarChart(); ch.type = "col"
        ch.add_data(Reference(ws, min_col=3, min_row=hdr, max_row=last), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
        _series_style(ch.series[0], NAVY)
        _style_chart(ch, f"Generation by unit, FY {fy} (MU)", "MU", width=18)
        ws.add_chart(ch, f"A{tr + 4}")
    _finish_sheet(ws, hdr, 8, [16, 16, 20, 11, 11, 15, 10, 16])
    ws.sheet_properties.tabColor = NAVY
    gens = [g for g in gens_full.values() if g is not None]
    checks.append(_check("Units add up to the station's total", sum(gens), cur["gen"], len(gens)))


def _sheet_matrix(wb: Workbook, d: dict, ex: dict, checks: list[dict]) -> None:
    ws = wb.create_sheet("Station x Month")
    fy, cur = d["fy"], d["kpis"]["cur"]
    months = ex["matrix_months"]
    hdr = _banner(ws, f"Station-wise monthly generation · KPCL · FY {fy}", "Gross generation in MU; every station in every month", len(months) + 4)
    _header(ws, hdr, ["Station", "Name"] + [_month_name(m[1]) for m in months] + ["FY total"], numeric_from=3)
    grid = ex["matrix"]
    r = hdr
    col_tot = [0.0] * len(months)
    for s in d["stations"]:
        r += 1
        vals = [grid.get((s["station"], m[0])) for m in months]
        _cell(ws, r, 1, s["station"], bold=True, fill=_band(r))
        _cell(ws, r, 2, s["name"], fill=_band(r))
        for i, v in enumerate(vals):
            _cell(ws, r, 3 + i, None if v is None else round(v, 3), _FMT_MU, fill=_band(r))
            col_tot[i] += v or 0
        _cell(ws, r, 3 + len(months), round(sum(v or 0 for v in vals), 3), _FMT_MU, bold=True, fill=_band(r))
    last = r
    r += 1
    _cell(ws, r, 1, "KPCL total"); _cell(ws, r, 2, None)
    for i, v in enumerate(col_tot):
        _cell(ws, r, 3 + i, round(v, 3), _FMT_MU)
    _cell(ws, r, 3 + len(months), round(sum(col_tot), 3), _FMT_MU)
    _total_style(ws, r, 3 + len(months))
    _finish_sheet(ws, hdr, len(months) + 3, [12, 34] + [11] * len(months) + [14], freeze_col="C")
    ws.sheet_properties.tabColor = NAVY
    monthly = {m["month"]: m["gen"] for m in d["monthly"]}
    diff = max((abs(col_tot[i] - (monthly.get(m[1]) or 0)) for i, m in enumerate(months)), default=0)
    tol = 0.0005 * len(d["stations"]) + 1e-9
    checks.append({"label": "Station x Month columns match the monthly totals", "ok": diff <= tol,
                   "detail": f"largest difference {diff:.3f} MU across {len(months)} months (allowed for rounding ±{tol:.3f})"})


def _sheet_trend(wb: Workbook, d: dict) -> None:
    s = d["series"]
    fys = [x["fy"] for x in s["gen"]]
    if not fys:
        return
    ws = wb.create_sheet("Ten-year trend")
    st = d["scope"]
    who = st["station"] if st else "KPCL"
    cols = [("Gross generation (MU)", "gen", _FMT_MU), ("Change on previous FY", None, _FMT_CHG_PCT), ("PLF (%)", "plf", _FMT_PCT)]
    if not st:
        cols += [("Thermal PLF (%)", "thermal_plf", _FMT_PCT), ("Hydro generation (MU)", "hydro_gen", _FMT_MU)]
    cols += [("CEA target achievement (%)", "achievement", _FMT_PCT), ("Auxiliary consumption (%)", "aux", _FMT_PCT)]
    hdr = _banner(ws, f"Financial-year trend · {who}", "Complete years, plus the selected year (marked) — the series behind the dashboard's trend chart", len(cols) + 2)
    _header(ws, hdr, ["Financial year"] + [c[0] for c in cols] + ["Note"], numeric_from=2)
    by = {k: {x["fy"]: x["v"] for x in s[k]} for k in s}
    r, prev_gen = hdr, None
    for f in fys:
        r += 1
        selected = f == d["fy"]
        _cell(ws, r, 1, f"FY {f}", bold=True, fill=GOLD_SOFT if selected else _band(r))
        for i, (_, key, fmt) in enumerate(cols, 2):
            v = _pct_change(by["gen"][f], prev_gen) if key is None else by[key].get(f)
            v = round(v, 3) if key in ("gen", "hydro_gen") and v is not None else v
            _cell(ws, r, i, v, fmt, fill=GOLD_SOFT if selected else _band(r))
        _cell(ws, r, len(cols) + 2, ("selected year" if selected else "") + (" · part year" if f not in d["complete_fys"] else ""), color=MUTED, size=9,
              fill=GOLD_SOFT if selected else _band(r))
        prev_gen = by["gen"][f]
    last = r
    _colour_change(ws, f"C{hdr + 1}:C{last}", f"C{hdr + 1}")
    bar = BarChart(); bar.type = "col"
    bar.add_data(Reference(ws, min_col=2, min_row=hdr, max_row=last), titles_from_data=True)
    bar.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=last))
    _series_style(bar.series[0], NAVY)
    _style_chart(bar, f"Gross generation by financial year, {who} (MU)", "MU", width=20)
    ws.add_chart(bar, f"A{last + 3}")
    _finish_sheet(ws, hdr, len(cols) + 2, [16] + [19] * len(cols) + [26])
    ws.sheet_properties.tabColor = GOLD


def _sheet_losses(wb: Workbook, d: dict) -> None:
    if not d["losses"]:
        return
    ws = wb.create_sheet("Generation loss")
    hdr = _banner(ws, f"Where generation was lost · FY {d['fy']}", "The largest reasons, by loss in MU; back-down is generation held back, outage is a unit out", 5)
    _header(ws, hdr, ["Rank", "Reason", "Type", "Loss (MU)", "Share of the reasons listed"], numeric_from=4)
    total = sum(x["loss"] or 0 for x in d["losses"])
    for i, x in enumerate(d["losses"]):
        r = hdr + 1 + i
        _cell(ws, r, 1, i + 1, "0", align="center", fill=_band(r))
        _cell(ws, r, 2, x["reason"], fill=_band(r), wrap=True)
        _cell(ws, r, 3, "Outage" if x["class"] == "OUTAGE" else "Back-down" if x["class"] == "BACKDOWN" else x["class"], align="center", fill=_band(r))
        _cell(ws, r, 4, x["loss"], "#,##0.0", fill=_band(r))
        _cell(ws, r, 5, x["loss"] / total if total else None, "0.0%", fill=_band(r))
    r = hdr + len(d["losses"]) + 1
    _cell(ws, r, 1, "Total"); _cell(ws, r, 2, "Reasons listed above"); _cell(ws, r, 3, None)
    _cell(ws, r, 4, round(total, 1), "#,##0.0"); _cell(ws, r, 5, 1 if total else None, "0.0%")
    _total_style(ws, r, 5)
    _finish_sheet(ws, hdr, 5, [8, 56, 14, 14, 24], freeze_col="A")
    ws.sheet_properties.tabColor = BAD


def _sheet_units_out(wb: Workbook, d: dict) -> None:
    ws = wb.create_sheet("Units out of service")
    hdr = _banner(ws, f"Units out of service · as on {_dmy(d['as_of'])}", "Units that were not running on the last date in the data", 8)
    _header(ws, hdr, ["Unit", "Station", "Type of plant", "Capacity (MW)", "Status", "Out since", "Days", "Detail"], numeric_from=4)
    if not d["units_out"]:
        _cell(ws, hdr + 1, 1, f"All {d['kpis']['units_total']} units were available.", color=GOOD, bold=True)
        ws.merge_cells(start_row=hdr + 1, start_column=1, end_row=hdr + 1, end_column=8)
    for i, u in enumerate(d["units_out"]):
        r = hdr + 1 + i
        vals = [u["unit"], u["station"], _CAT_LABEL.get(u["category"], u["category"]), u["capacity_mw"], (u["status"] or "").replace("_", " ").capitalize(),
                _dmy(u["since"]) if u["since"] else "—", u["days"], u["detail"] or ""]
        for col, (v, f) in enumerate(zip(vals, [None, None, None, "#,##0.0", None, None, _FMT_INT, None]), 1):
            _cell(ws, r, col, v, f, bold=col == 1, fill=_band(r), wrap=col == 8, align="center" if col in (5, 6) else None)
    if d["units_out"]:
        r = hdr + len(d["units_out"]) + 1
        _cell(ws, r, 1, f"{len(d['units_out'])} units")
        for col in range(2, 9):
            _cell(ws, r, col, None)
        _cell(ws, r, 4, sum(u["capacity_mw"] or 0 for u in d["units_out"]), "#,##0.0")
        _total_style(ws, r, 8)
    _finish_sheet(ws, hdr, 8, [16, 12, 16, 15, 20, 14, 9, 60], freeze_col="A")
    ws.sheet_properties.tabColor = BAD


def _sheet_coal(wb: Workbook, d: dict) -> None:
    if not d["coal"]:
        return
    ws = wb.create_sheet("Coal stock")
    hdr = _banner(ws, f"Coal stock · as on {_dmy(d['as_of'])}", "Closing stock at each thermal station against its normative stock", 5)
    _header(ws, hdr, ["Station", "Closing stock (tonnes)", "Days of stock", "Normative days", "Position"], numeric_from=2)
    for i, c in enumerate(d["coal"]):
        r = hdr + 1 + i
        _cell(ws, r, 1, c["station"], bold=True, fill=_band(r))
        _cell(ws, r, 2, c["stock_mt"], _FMT_INT, fill=_band(r))
        _cell(ws, r, 3, c["days"], "0.0", fill=_band(r))
        _cell(ws, r, 4, c["norm"], "0", fill=_band(r))
        _cell(ws, r, 5, "Below norm" if c["below"] else "Within norm", align="center", bold=True, color=BAD if c["below"] else GOOD,
              fill=BAD_SOFT if c["below"] else GOOD_SOFT)
    r = hdr + len(d["coal"]) + 3
    ws.cell(r - 1, 1, f"Average days of stock by month, FY {d['fy']}").font = _f(size=11, bold=True, color=NAVY)
    _header(ws, r, ["Month", "Average days of stock", "Normative days"], numeric_from=2)
    for i, t in enumerate(d["coal_trend"]):
        rr = r + 1 + i
        _cell(ws, rr, 1, t["label"], bold=True, fill=_band(rr)); _cell(ws, rr, 2, t["days"], "0.0", fill=_band(rr)); _cell(ws, rr, 3, t["norm"], "0", fill=_band(rr))
    if d["coal_trend"]:
        ch = LineChart()
        ch.add_data(Reference(ws, min_col=2, max_col=3, min_row=r, max_row=r + len(d["coal_trend"])), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=r + 1, max_row=r + len(d["coal_trend"])))
        _series_style(ch.series[0], NAVY, line=True)
        _series_style(ch.series[1], BAD, line=True, dash="dash")
        _style_chart(ch, "Average days of coal stock", "days", width=18, height=8)
        ws.add_chart(ch, f"G{hdr}")
    _finish_sheet(ws, hdr, 5, [16, 22, 16, 16, 16], freeze_col="A", print_table_only=True)
    ws.sheet_properties.tabColor = GOLD


def _sheet_reservoirs(wb: Workbook, d: dict) -> None:
    if not d["reservoirs"]:
        return
    ws = wb.create_sheet("Reservoirs")
    hdr = _banner(ws, f"Reservoir levels · as on {_dmy(d['as_of'])}", "Water level against full reservoir level (FRL) and minimum draw-down level (MDDL)", 8)
    _header(ws, hdr, ["Reservoir", "Water level", "FRL", "MDDL", "Level unit", "Storage (% of capacity)", "Energy potential (MU)", "Feeds"], numeric_from=2)
    for i, x in enumerate(d["reservoirs"]):
        r = hdr + 1 + i
        _cell(ws, r, 1, (x["name"] or "").title(), bold=True, fill=_band(r))
        _cell(ws, r, 2, x["level"], "#,##0.00", fill=_band(r)); _cell(ws, r, 3, x["frl"], "#,##0.00", fill=_band(r))
        _cell(ws, r, 4, x["mddl"], "#,##0.00", fill=_band(r)); _cell(ws, r, 5, (x["unit"] or "").lower(), align="center", fill=_band(r))
        _cell(ws, r, 6, x["pct"], "0.0", fill=_band(r)); _cell(ws, r, 7, x["energy_mu"], _FMT_INT, fill=_band(r))
        _cell(ws, r, 8, x["feeds"] or "", fill=_band(r))
    _finish_sheet(ws, hdr, 8, [22, 14, 12, 12, 11, 22, 22, 34], freeze_col="A")
    ws.sheet_properties.tabColor = "2F8FD6"


_NOTES = [
    ("Purpose", "A snapshot of the KPCL Generation Dashboard for the selection named on the Summary sheet, in a form that can be filed, printed or filtered. "
                "Every figure is produced by the same fixed, read-only queries as the dashboard, so the two always agree."),
    ("Financial year", "1 April to 31 March. A year for which the data ends early is marked as a part year; comparisons with a full previous year are then not shown."),
    ("Gross generation (MU)", "Sum of gross generation in million units for the scope and period."),
    ("PLF (%)", "Plant load factor = sum of gross generation ÷ sum of capacity-based potential generation × 100. Always recomputed from summed totals, never averaged from monthly or unit percentages."),
    ("PAF (%)", "Plant availability factor = sum of declared capacity ÷ sum of capacity-based potential generation × 100 (unit sheet)."),
    ("CEA target achievement (%)", "Gross generation of the stations or months that have a CEA target ÷ the CEA target × 100. CEA targets are set monthly, so weekly and daily sheets carry none."),
    ("Auxiliary consumption (%)", "Energy used by the station itself ÷ gross generation × 100 (lower is better)."),
    ("Weeks", "Calendar weeks, Monday to Sunday. The first and last weeks of a financial year can be part weeks. A week is compared with the week 364 days earlier so that weekdays line up. "
              "A week belongs to the month in which its middle day falls."),
    ("Days", "A day is compared with the same date one year earlier (29 February has no partner)."),
    ("As on", "Unit status, coal stock and reservoir levels are positions on the last date in the data, not figures for the financial year."),
    ("Change", "Percentage change is (current − previous) ÷ previous × 100; for ratios (PLF, achievement, auxiliary consumption, share) the change is in percentage points."),
    ("Rounding", "Values are written to 3 decimals (MU) and formatted to 2. Integrity checks allow for that rounding and nothing more."),
    ("Data status", "Proof-of-concept data. Only the RTPS and SGS figures pinned to K-eRP reports are real; the rest is illustrative."),
]


def _sheet_notes(wb: Workbook, d: dict, generated: dt.datetime) -> None:
    ws = wb.create_sheet("Notes")
    hdr = _banner(ws, "Notes and definitions", "How the figures in this workbook are defined and computed", 2)
    _header(ws, hdr, ["Item", "Definition"])
    for i, (a, b) in enumerate(_NOTES):
        r = hdr + 1 + i
        _cell(ws, r, 1, a, bold=True, fill=_band(r), wrap=True)
        _cell(ws, r, 2, b, fill=_band(r), wrap=True)
        ws.row_dimensions[r].height = 15 * max(1, -(-len(b) // 105)) + 4
    r = hdr + len(_NOTES) + 2
    ws.cell(r, 1, f"Generated {generated.strftime('%d-%b-%Y %H:%M')} from the KPCL Generation Management System dashboard data (data to {_dmy(d['as_of'])}).").font = _f(size=9, italic=True, color=MUTED)
    _finish_sheet(ws, hdr, 2, [30, 112], freeze_col="A")
    ws.sheet_properties.tabColor = MUTED


# ------------------------------------------------------------------------------------------------ build
def _make_workbook(d: dict, ex: dict, grain: str, generated: dt.datetime, asked: str | None = None,
                   notes: list[tuple[str, str]] | None = None, version: str = "") -> Workbook:
    wb = Workbook()
    checks: list[dict] = []
    grains = ("monthly", "weekly", "daily") if grain == "all" else (grain,)
    for g in grains:
        {"monthly": _sheet_monthly, "weekly": _sheet_weekly, "daily": _sheet_daily}[g](wb, d, checks)
    if d["scope"]:
        _sheet_units(wb, d, ex, checks)
    else:
        _sheet_stations(wb, d, ex, checks)
        _sheet_matrix(wb, d, ex, checks)
    _sheet_trend(wb, d)
    _sheet_losses(wb, d)
    _sheet_units_out(wb, d)
    _sheet_coal(wb, d)
    _sheet_reservoirs(wb, d)
    _sheet_notes(wb, d, generated)
    _sheet_summary(wb, d, grain, checks, generated, asked, notes, version)
    wb.move_sheet("Summary", offset=-(len(wb.sheetnames) - 1))
    wb.active = 0
    wb.properties.title = f"KPCL MIS report — {d['scope']['station'] if d['scope'] else 'all stations'} — FY {d['fy']}"
    wb.properties.creator = "KPCL Generation Management System"
    return finalize(wb, checks)


def finalize(wb: Workbook, checks: list[dict]) -> Workbook:
    """The last step for every report workbook, whether it is downloaded or previewed:

      * no cell is ever a formula: this app writes values only, so any text a spreadsheet would treat as a formula
        (a question typed as =HYPERLINK(...), a label from a model-written query) is stored as plain text and marked
        with Excel's quote prefix, so it stays text even when someone edits the cell;
      * every sheet's printed footer, and the file's properties, say it is a proof of concept, not for official use;
      * the integrity checks travel with the workbook (wb.kpcl_checks) so the server can refuse a failing one.
    """
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str) and (c.data_type == "f" or v.startswith(_FORMULA_START)):
                    c.data_type = "s"
                    c.quotePrefix = True
        if not ws.oddFooter.center.text:
            ws.oddFooter.center.text = DRAFT_NOTE
        if not ws.oddFooter.left.text:
            ws.oddFooter.left.text = "KPCL · Generation Management System"
    pr = wb.properties
    if not (pr.title or "").startswith("DRAFT"):
        pr.title = f"DRAFT – {pr.title or 'KPCL report'}"
    pr.subject = DRAFT_NOTE
    pr.description = f"{DRAFT_NOTE}. Figures from the KPCL Generation Management System proof-of-concept data."
    pr.keywords = "draft; proof of concept; not for official use"
    pr.category = "Draft – proof of concept"
    wb.kpcl_checks = list(checks)
    return wb


def failed_checks(wb: Workbook) -> list[dict]:
    return [c for c in getattr(wb, "kpcl_checks", []) if c.get("ok") is False]


def data_version(db: ReadOnlyDB) -> str:
    """Short stamp of the warehouse file (changes whenever the warehouse is rebuilt)."""
    import hashlib
    st = db.path.stat()
    return hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:8]


def workbook_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()




async def _extras(db: ReadOnlyDB, d: dict, timeout: float) -> dict:
    ex: dict = {}

    async def q(sql: str, params: tuple):
        return dd._dicts(await db.run(sql, max_rows=5000, timeout_seconds=timeout, restricted=True, params=params))

    fy, pfy = d["fy"], d["prev_fy"]
    if d["scope"]:
        # the dashboard rounds each unit's generation to 1 decimal; the report keeps 3 so that the units add up to the station
        ex["units"] = {r["unit_key"]: r["gen"] for r in await q(dd._UNIT_TABLE_SQL, (fy, d["scope"]["station"]))}
        return ex
    ex["stations"] = {r["station_key"]: r for r in await q(dd._STATION_TABLE_SQL, (pfy or fy, fy))}
    matrix = await q(_MATRIX_SQL, (fy,))
    ex["matrix"] = {(r["station_key"], r["year_month"]): r["gen"] for r in matrix}
    seen: dict[str, str] = {}
    for r in matrix:
        seen.setdefault(r["year_month"], r["month_label"])
    ex["matrix_months"] = sorted(seen.items())
    return ex


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


async def build_mis_report(db: ReadOnlyDB, fy: str | None, station: str | None, grain: str, timeout_seconds: float,
                           asked: str | None = None, as_workbook: bool = False,
                           notes: list[tuple[str, str]] | None = None) -> tuple[bytes | Workbook, str]:
    """(xlsx bytes, file name), or (the openpyxl Workbook, file name) for the on-screen viewer. Unknown FY, station or
    grain raise DashboardNotFound (-> 404), never a query; a workbook that fails an integrity check raises
    ReportUnderReview (-> 409) and is never handed out."""
    if grain not in GRAINS:
        raise dd.DashboardNotFound(f"No such period view {grain!r}.")
    d = await dd.build_dashboard(db, fy, station, timeout_seconds)
    ex = await _extras(db, d, timeout_seconds)
    generated = dt.datetime.now()
    wb = await asyncio.to_thread(_make_workbook, d, ex, grain, generated, asked, notes, data_version(db))
    failed = failed_checks(wb)
    if failed:
        raise ReportUnderReview(failed)
    data = wb if as_workbook else await asyncio.to_thread(workbook_bytes, wb)
    name = f"KPCL_MIS_{d['scope']['station'] if d['scope'] else 'All-stations'}_FY{d['fy']}_{grain.capitalize()}.xlsx"
    return data, _slug(name)
