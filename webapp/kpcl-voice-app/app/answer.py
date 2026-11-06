"""
Deterministic answer writing -- no second LLM call (port of the WB POC's answer writer, KPCL units):

  * one-row result  -> the model's answer_template filled from the row (and scrubbed), or labelled figures;
  * many-row result -> plain-language narration: who leads, share of the total (only for additive measures),
                       spread, first-to-last change for time series, then a short list or a pointer to the table;
  * every figure is formatted by its unit (MU, %, kcal/kWh, tonnes ...) taken from the semantic layer, with
    name-based fallbacks for aliases the model invents;
  * caveats from the known-issues list are attached when the SQL or result touches what they affect.
"""
from __future__ import annotations

import re

from app import guardrails
from app.semantic_layer import column_info, load_semantic_layer

LIST_LIMIT = 6
TABLE_ROW_CAP = 500
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_STRUCTURAL = {"RTPS-U1": "out since 10-Aug-2022", "RTPS-U4": "out since 09-Nov-2024", "SIRWAR-U1": "out since 01-Jun-2023"}

# ---------------------------------------------------------------------------------------------- units
_UNIT_BY_NAME = [
    (r"(pct|percent|share|achievement|^plf|^paf|_plf|_paf|plf_|paf_)", "%"),
    (r"heat_rate|_hr_kcal", "kcal/kWh"), (r"gcv", "kcal/kg"), (r"sp_coal|kg_kwh", "kg/kWh"), (r"sp_oil|ml_kwh", "ml/kWh"),
    (r"(_mu$|_mu_|^mu$|gen_mu|mu_)", "MU"), (r"(_mt$|tonnes|quantity)", "tonnes"), (r"_kl$", "kL"),
    (r"(_hrs$|hours)", "hours"), (r"_mw$", "MW"), (r"(days|_days$)", "days"),
    (r"(count|outages$|trips$|failures$|rakes$|events$|^n_|number)", "count"), (r"_mcft$", "MCFT"), (r"_mm$", "mm"),
]
_PRECISION = {"MU": 3, "%": 2, "kcal/kWh": 0, "kcal/kg": 0, "kcal/litre": 0, "kg/kWh": 3, "ml/kWh": 3, "tonnes": 0,
              "kL": 2, "hours": 2, "MW": 2, "days": 1, "count": 0, "MCFT": 2, "mm": 1, "Hz": 2, "MMSCM": 4,
              "cubic metres": 0}
# Whole name parts only ("rate" must not match inside "generation", "age" inside "average" or "outage").
_NON_ADDITIVE_NAME = re.compile(r"(^|_)(pct|percent|share|achievement|rate|ratio|avg|average|mean|median|per|max|min|"
                                r"level|gcv|heat|sp|plf|paf|peak|index|frequency|hz|status|since|age|duration|"
                                r"in_service|cum)(_|$)|days_of_stock|capacity_mw", re.I)


# Positions on a date (stock, balance, level, storage): additive across stations on one day, never across days.
_BALANCE_NAME = re.compile(r"(^|_)(stock|closing|opening|balance|storage|level|levels|days_of_stock|capacity_pct)(_|$)", re.I)


def summable_over_time(col: str) -> bool:
    return is_additive(col) and not _BALANCE_NAME.search(col)


def col_unit(col: str) -> str | None:
    info = column_info().get(col)
    if info and info.get("unit"):
        return info["unit"]
    c = col.lower()
    for pat, unit in _UNIT_BY_NAME:
        if re.search(pat, c):
            return unit
    return None


def is_additive(col: str) -> bool:
    info = column_info().get(col)
    if info and info.get("additive") is not None:
        return bool(info["additive"]) and col not in load_semantic_layer()["non_summable_columns"]
    unit = col_unit(col)
    if unit in ("%", "kcal/kWh", "kcal/kg", "kg/kWh", "ml/kWh", "MW", "Hz"):
        return False
    return not _NON_ADDITIVE_NAME.search(col)


def to_float(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        try:
            return float(s) if s else None
        except ValueError:
            return None
    return None


def fmt_number(v: float, places: int) -> str:
    if places <= 0:
        return f"{v:,.0f}"
    return f"{v:,.{places}f}"


def fmt_value(col: str, v) -> str:
    """A figure with its unit: 8,310.351 MU / 64.95% / 2,452 kcal/kWh."""
    if v is None:
        return "no data"
    num = to_float(v)
    if num is None:
        return str(v)
    unit = col_unit(col)
    if unit is None:
        places = 0 if float(num).is_integer() else 2
        return fmt_number(num, places)
    text = fmt_number(num, _PRECISION.get(unit, 2))
    if unit == "%":
        return text + "%"
    if unit == "count":
        return text
    return f"{text} {unit}"


# ---------------------------------------------------------------------------------------------- labels
_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ML_RE = re.compile(r"^([A-Z]{3})-(\d{4})$")
_WORDS = {"pct": "%", "mu": "(MU)", "mw": "(MW)", "hrs": "hours", "mt": "(t)", "kl": "(kL)", "fy": "FY", "plf": "PLF",
          "paf": "PAF", "cea": "CEA", "sp": "specific", "wa": "weighted", "kcal": "kcal", "kwh": "kWh", "kg": "kg",
          "gcv": "GCV", "nld": "NLD", "btf": "BTF", "yoy": "YoY", "key": "", "label": "", "gen": "generation",
          "aux": "aux", "max": "max", "no": "no."}


def is_fy(s: str) -> bool:
    m = _FY_RE.match(s)
    return bool(m) and (int(m.group(1)) + 1) % 100 == int(m.group(2))


def time_key(s) -> tuple | None:
    """Sort key when the value is a period label, else None."""
    s = str(s or "").strip()
    if _DATE_RE.match(s):
        return (s,)
    if is_fy(s):
        return (s[:4] + "-04",)
    m = _YM_RE.match(s)
    if m and 1 <= int(m.group(2)) <= 12:
        return (s,)
    m = _ML_RE.match(s)
    if m and m.group(1).title() in _MONTH_ABBR:
        return (f"{m.group(2)}-{_MONTH_ABBR.index(m.group(1).title()) + 1:02d}",)
    return None


def pretty_label(v) -> str:
    s = "" if v is None else str(v)
    m = _YM_RE.match(s)
    if m and not is_fy(s) and 1 <= int(m.group(2)) <= 12:
        return f"{_MONTH_ABBR[int(m.group(2)) - 1]} {m.group(1)}"
    m = _ML_RE.match(s)
    if m and m.group(1).title() in _MONTH_ABBR:
        return f"{m.group(1).title()} {m.group(2)}"
    if is_fy(s):
        return f"FY {s}"
    m = _DATE_RE.match(s)
    if m:
        return f"{int(m.group(3)):02d}-{_MONTH_ABBR[int(m.group(2)) - 1]}-{m.group(1)}"
    if s.isupper() and "_" in s and not re.search(r"\d", s):
        return s.replace("_", "-").capitalize()          # POST_MONSOON -> Post-monsoon
    if s.isupper() and len(s) > 4 and " " in s and "-" not in s:
        return s.title()
    return s or "Unspecified"


def header(col: str) -> str:
    words = [_WORDS.get(w, w) for w in col.lower().split("_")]
    text = " ".join(w for w in words if w).strip()
    return (text[:1].upper() + text[1:]) if text else col


def in_sentence(col: str) -> str:
    """The column's name inside a sentence: 'gross generation (MU)', but 'PLF %' keeps its acronym."""
    h = header(col)
    return h[:1].lower() + h[1:] if len(h) > 1 and h[1].islower() else h


_UNIT_NOUN = [("station", "stations"), ("unit", "units"), ("month", "months"), ("year_month", "months"),
              ("fy", "financial years"), ("water_year", "water years"), ("quarter", "quarters"), ("season", "seasons"),
              ("reason", "reasons"), ("supplier", "suppliers"), ("category", "categories"), ("location", "locations"),
              ("reservoir", "reservoirs"), ("date", "days"), ("day", "day types"), ("type", "types"), ("period", "periods")]


def _noun(label_col: str, time_series: bool, sample: str) -> str:
    if time_series:
        if _DATE_RE.match(sample):
            return "days"
        if is_fy(sample):
            return "financial years"
        return "months"
    c = label_col.lower()
    for hint, noun in _UNIT_NOUN:
        if hint in c:
            return noun
    return "rows"


_ENTITY_COL_RE = re.compile(r"(^|_)(unit|station|reservoir|supplier|reason|category)(_key|_name|_code)?$|_key$|^name$")


def _pick_label(columns: list[str], rows: list[tuple]) -> int:
    varying = {i for i in range(len(columns)) if len({r[i] for r in rows}) > 1} if len(rows) > 1 else set(range(len(columns)))
    # One row per unit / station / reservoir: that is the label, even if a date column (commissioning date, outage
    # start) also varies -- such a date is an attribute of the row, not a time axis.
    if len(rows) > 1:
        for i, c in enumerate(columns):
            if _ENTITY_COL_RE.search(c.lower()) and len({r[i] for r in rows}) == len(rows) and to_float(rows[0][i]) is None:
                return i
    for i in sorted(varying):
        if all(time_key(r[i]) for r in rows):
            return i
    for i, c in enumerate(columns):
        if i in varying and to_float(rows[0][i]) is None and not c.lower().endswith(("_code",)):
            return i
    for i in range(len(columns)):
        if to_float(rows[0][i]) is None:
            return i
    return 0


def _metric_cols(columns: list[str], label_idx: int, rows: list[tuple]) -> list[int]:
    out = []
    for i, c in enumerate(columns):
        if i == label_idx or to_float(rows[0][i]) is None and all(to_float(r[i]) is None for r in rows):
            continue
        cl = c.lower()
        if cl.endswith(("_key", "_code", "_no", "_id")) or cl in ("fy", "year", "month", "rank", "is_weekend"):
            continue
        vals = {to_float(r[i]) for r in rows} - {None}
        if len(rows) >= 3 and vals and vals <= {0.0, 1.0} and cl.startswith(("is_", "has_")):
            continue
        out.append(i)
    return out


# ---------------------------------------------------------------------------------------------- narration
def _median(values: list[float]) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _narrate_primary(col: str, points: list[tuple[str, float]], noun: str, time_series: bool) -> str:
    name = in_sentence(col)
    fmt = lambda v: fmt_value(col, v)  # noqa: E731
    n = len(points)
    values = [v for _, v in points]
    hi_lbl, hi_v = max(points, key=lambda p: p[1])
    lo_lbl, lo_v = min(points, key=lambda p: p[1])
    additive = (summable_over_time(col) if time_series else is_additive(col)) and all(v >= 0 for v in values)
    total = sum(values)
    if time_series:
        first_lbl, first_v = points[0]
        last_lbl, last_v = points[-1]
        if additive and total > 0:
            text = (f"Over the {n} {noun} from {first_lbl} to {last_lbl}, {name} totalled {fmt(total)}. It was highest in "
                    f"{hi_lbl} ({fmt(hi_v)}) and lowest in {lo_lbl} ({fmt(lo_v)}).")
        else:
            text = f"Across the {n} {noun} from {first_lbl} to {last_lbl}, {name} ranged from {fmt(lo_v)} in {lo_lbl} to {fmt(hi_v)} in {hi_lbl}."
        if first_v:
            change = 100 * (last_v - first_v) / abs(first_v)
            if abs(change) < 1:
                text += f" The latest ({last_lbl}) is about level with the first ({first_lbl})."
            else:
                text += (f" The latest ({last_lbl}, {fmt(last_v)}) is {abs(change):.1f}% {'higher' if change > 0 else 'lower'} "
                         f"than the first ({first_lbl}, {fmt(first_v)}).")
        return text
    ranked = sorted(points, key=lambda p: p[1], reverse=True)
    if n >= 3 and hi_v == lo_v:
        return f"The {name} is {fmt(hi_v)} for every one of the {n} {noun}."
    if additive and total > 0:
        text = f"{hi_lbl} has the highest {name}, {fmt(hi_v)}, which is {100 * hi_v / total:.1f}% of the {fmt(total)} total across {n} {noun}."
        if n >= 3:
            top3 = sum(v for _, v in ranked[:3])
            text += (f" It is followed by {ranked[1][0]} ({fmt(ranked[1][1])}) and {ranked[2][0]} ({fmt(ranked[2][1])}); "
                     f"together the top three account for {100 * top3 / total:.1f}%.")
            if n > 4:
                rest = ranked[3:]
                text += (f"\n\nThe other {len(rest)} {noun} range from {fmt(rest[0][1])} ({rest[0][0]}) down to "
                         f"{fmt(lo_v)} ({lo_lbl}).")
        elif n == 2:
            text += f" {lo_lbl} has {fmt(lo_v)}."
        return text
    mean = total / n
    text = f"{hi_lbl} has the highest {name}, {fmt(hi_v)}, and {lo_lbl} the lowest, {fmt(lo_v)}."
    if n >= 4:
        above = sum(1 for v in values if v > mean)
        text += f" Across the {n} {noun} the median is {fmt(_median(values))}; {above} {'is' if above == 1 else 'are'} above the mean."
    return text


def _narrate_secondary(col: str, points: list[tuple[str, float]], leader: str, time_series: bool) -> str:
    name = in_sentence(col)
    fmt = lambda v: fmt_value(col, v)  # noqa: E731
    values = [v for _, v in points]
    hi_lbl, hi_v = max(points, key=lambda p: p[1])
    lo_lbl, lo_v = min(points, key=lambda p: p[1])
    if hi_v == lo_v:
        return ""
    if (summable_over_time(col) if time_series else is_additive(col)) and all(v >= 0 for v in values):
        total = sum(values)
        if time_series:
            return f"The {name} totalled {fmt(total)}, highest in {hi_lbl} ({fmt(hi_v)})."
        if hi_lbl == leader:
            return f"{hi_lbl} also leads on {name}, with {fmt(hi_v)} of {fmt(total)}."
        return f"On {name} ({fmt(total)} in all), {hi_lbl} is highest at {fmt(hi_v)}."
    return f"The {name} ranges from {fmt(lo_v)} ({lo_lbl}) to {fmt(hi_v)} ({hi_lbl})."


_SHORTFALL_COL_RE = re.compile(r"shortfall|deficit|short_by|below_target|gap", re.IGNORECASE)


def _shortfalls_as_positive(columns: list[str], rows: list[tuple]) -> list[tuple]:
    """A shortfall written as actual minus target is negative; "highest shortfall" must mean the biggest miss, so
    such a column is narrated by its size (Bellary short by 49.8 MU is the largest, not the smallest)."""
    flip = set()
    for i, c in enumerate(columns):
        if _SHORTFALL_COL_RE.search(c):
            vals = [to_float(r[i]) for r in rows if to_float(r[i]) is not None]
            if vals and max(vals) <= 0 and min(vals) < 0:
                flip.add(i)
    if not flip:
        return rows
    return [tuple(abs(to_float(v)) if i in flip and to_float(v) is not None else v for i, v in enumerate(r)) for r in rows]


def explain_rows(columns: list[str], rows: list[tuple], preferred: str | None = None) -> str:
    """A multi-row result written for a reader."""
    rows = _shortfalls_as_positive(columns, rows)
    label_idx = _pick_label(columns, rows)
    metrics = _metric_cols(columns, label_idx, rows)
    if preferred in columns and columns.index(preferred) in metrics:
        metrics.remove(columns.index(preferred))
        metrics.insert(0, columns.index(preferred))
    time_series = all(time_key(r[label_idx]) for r in rows)
    if time_series:
        ordered = sorted(rows, key=lambda r: time_key(r[label_idx]))
    elif metrics:
        ordered = sorted(rows, key=lambda r: -(to_float(r[metrics[0]]) if to_float(r[metrics[0]]) is not None else -1e18))
    else:
        ordered = list(rows)
    noun = _noun(columns[label_idx], time_series, str(ordered[0][label_idx]))
    parts = []
    if metrics:
        pts = [(pretty_label(r[label_idx]), to_float(r[metrics[0]])) for r in ordered]
        pts = [(lbl, v) for lbl, v in pts if v is not None]
        if len(pts) >= 2:
            parts.append(_narrate_primary(columns[metrics[0]], pts, noun, time_series))
            leader = max(pts, key=lambda p: p[1])[0]
            extra = []
            for i in metrics[1:3]:
                p2 = [(pretty_label(r[label_idx]), to_float(r[i])) for r in ordered]
                p2 = [(lbl, v) for lbl, v in p2 if v is not None]
                if len(p2) >= 2:
                    extra.append(_narrate_secondary(columns[i], p2, leader, time_series))
            if any(extra):
                parts.append(" ".join(e for e in extra if e))
    if len(rows) <= LIST_LIMIT:
        lines = []
        for r in ordered:
            figs = "  ·  ".join(f"{header(columns[i])}: {fmt_value(columns[i], r[i])}" for i in metrics)
            lines.append(f"•  {pretty_label(r[label_idx])}" + (f" — {figs}" if figs else ""))
        parts.append("\n".join(lines))
    else:
        parts.append(f"All {len(rows)} {noun} are in the table below.")
    shut = [str(r[label_idx]) for r in ordered if str(r[label_idx]) in _STRUCTURAL]
    if shut and metrics:
        parts.append("Note: " + "; ".join(f"{u} has been {_STRUCTURAL[u]}" for u in shut) + ", so it generates nothing.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------------------------- templates
_FORMAT_SPEC_RE = re.compile(r"(\{[a-zA-Z_][a-zA-Z0-9_]*)(:[^}]*)(\})")
# The filler already appends the unit; a template that also writes it gives "831 MU MU" / "64.95% %".
_DUP_UNIT_RE = re.compile(r"\b(MU|MW|tonnes|kL|hours|days|MCFT|kcal/kWh|kcal/kg|kg/kWh|ml/kWh)(\s+(?:MU|MW|tonnes|tons|t|kL|"
                          r"hours|hrs|days|MCFT|kcal/kWh|kcal/kg|kg/kWh|ml/kWh|million units)\b)+", re.IGNORECASE)
_DUP_PCT_RE = re.compile(r"%(\s*(?:%|percent\b|per cent\b|pct\b))+", re.IGNORECASE)


def _dedupe_units(text: str) -> str:
    return _DUP_PCT_RE.sub("%", _DUP_UNIT_RE.sub(r"\1", text))


_PERIOD_SLOT_RE = r"\b(in|for|during|on|as of|as on|at the end of|by)\s+\{%s\}"


def _repair_repeated_slots(template: str, columns: list[str], entities: dict) -> str | None:
    """The model sometimes writes one value slot twice, once where the period belongs ("The storage at X in
    {storage_pct} was {storage_pct}"). With exactly one period named, the slot after "in / for / during" becomes the
    period; if a value slot is still repeated, the template can't be trusted (None: show the plain figures)."""
    names = re.findall(r"\{([a-zA-Z_]\w*)\}", template)
    dups = {n for n in names if n in columns and names.count(n) > 1}
    if not dups:
        return template
    periods = [p for p in entities.get("periods", []) if p.get("label") and p.get("kind") != "out_of_range"]
    for n in dups:
        if len(periods) == 1:
            template = re.sub(_PERIOD_SLOT_RE % re.escape(n), lambda m: f"{m.group(1)} {periods[0]['label']}", template,
                              count=1, flags=re.IGNORECASE)
        if template.count("{" + n + "}") > 1:
            return None
    return template


def fill_template(template: str, columns: list[str], rows: list[tuple], entities: dict,
                  preferred: str | None = None) -> str:
    if not rows or (len(rows) == 1 and all(v is None for v in rows[0])):
        return ("No matching data was found for that question. The combination may not exist in the data "
                "(for example a unit before its commissioning, or a period outside Apr-2015 to Apr-2025).")
    if len(rows) == 1:
        values = {c: fmt_value(c, v) for c, v in zip(columns, rows[0])}
        flat = {k: v for k, v in entities.items() if isinstance(v, (str, int, float))}
        try:
            tpl = _repair_repeated_slots(_FORMAT_SPEC_RE.sub(r"\1\3", template or ""), columns, entities)
            if tpl is None:
                raise ValueError("repeated value slot")
            filled = _dedupe_units(guardrails.scrub(tpl.format(**flat, **values)))
            if filled and "no data" not in filled:
                return filled
        except (KeyError, IndexError, ValueError):
            pass
        shown = [(c, v) for c, v in zip(columns, rows[0]) if v not in (None, "")]
        return "\n".join(f"{header(c)}: {fmt_value(c, v)}" for c, v in shown)
    if len(columns) >= 2:
        return explain_rows(columns, rows, preferred)
    values = [pretty_label(r[0]) for r in rows]
    if len(values) <= LIST_LIMIT:
        return f"{len(values)} results:\n" + "\n".join(f"•  {v}" for v in values)
    return f"{len(values)} results, including {', '.join(values[:5])} and {len(values) - 5} more (all in the table below)."


_FIRST_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?=\s|$)", re.S)


def present(text: str) -> str:
    """Every data answer leads with its point in bold."""
    if not text or text.startswith("**") or text.startswith("No matching data"):
        return text
    first = text.split("\n", 1)[0]
    m = _FIRST_SENTENCE_RE.match(first)
    if not m or len(m.group(1)) > 400 or first.startswith("•"):
        return text
    return f"**{m.group(1)}**" + text[len(m.group(1)):]


# ---------------------------------------------------------------------------------------------- caveats
def caveats(sql: str, columns: list[str], rows: list[tuple], entities: dict | None = None) -> list[str]:
    """Known-issue notes that apply to this answer (never the global 'synthetic data' one; the UI shows that)."""
    sql_l = (sql or "").lower()
    cells = {str(v) for r in rows[:500] for v in r}
    notes = []
    for c in load_semantic_layer()["caveats"]:
        hit = False
        for a in c["affects"]:
            if a == "*":
                continue
            if "=" in a:
                col, val = a.split("=", 1)
                if val.lower() in sql_l or val in cells or (col == "fy" and any(
                        p.get("value") == val for p in (entities or {}).get("periods", []))):
                    hit = True
            elif a.lower() in sql_l:
                hit = hit or "." not in a or a.split(".", 1)[1].lower() in sql_l
        if c["id"] == "KI-04" and "2025-04-06" not in sql_l:
            hit = False
        if hit and c["caveat"] not in notes:
            notes.append(c["caveat"])
    return notes


# ---------------------------------------------------------------------------------------------- table / chart
def result_table(columns: list[str], rows: list[tuple]) -> dict | None:
    if len(rows) < 2 or not columns:
        return None
    kinds = []
    for i, c in enumerate(columns):
        numeric = any(to_float(r[i]) is not None for r in rows[:20]) and not c.lower().endswith(("_key", "_code"))
        kinds.append("number" if numeric else "text")
    out_rows = []
    for r in rows[:TABLE_ROW_CAP]:
        out_rows.append([fmt_value(c, v) if k == "number" and v is not None else (pretty_label(v) if v is not None else "")
                         for c, v, k in zip(columns, r, kinds)])
    return {"headers": [header(c) + (f" ({col_unit(c)})" if col_unit(c) and col_unit(c) not in ("count",) and
                                     col_unit(c) not in header(c) else "") for c in columns],
            "kinds": kinds, "rows": out_rows, "total_rows": len(rows)}


def build_chart(chart_type: str, label_col: str, value_col: str, series_label: str, columns: list[str],
                rows: list[tuple]) -> dict | None:
    if not rows or label_col not in columns or value_col not in columns or len(rows) < 2:
        return None
    li, vi = columns.index(label_col), columns.index(value_col)
    ordered = sorted(rows, key=lambda r: time_key(r[li])) if all(time_key(r[li]) for r in rows) else rows
    values = [to_float(r[vi]) for r in ordered]
    if all(v is None for v in values):
        return None
    return {"type": chart_type if chart_type in ("bar", "line") else "bar", "labels": [pretty_label(r[li]) for r in ordered],
            "values": values, "series_label": series_label or header(value_col), "unit": col_unit(value_col) or ""}


# ---------------------------------------------------------------------------------------------- unit conversion
# (phrase in the question, unit written in the answer, factor, unit label, decimals). The first phrase found wins;
# every figure written in that unit in the answer gets the converted value added in brackets. Figures are never
# replaced, so the reviewed number the data holds is always still there.
_CONVERSIONS = [
    (r"\bgwh\b|\bgigawatt[ -]?hours?\b", "MU", 1.0, "GWh", 3),
    (r"\btwh\b|\bterawatt[ -]?hours?\b", "MU", 0.001, "TWh", 3),
    (r"\bbillion (units|kwh)\b|\bbu\b", "MU", 0.001, "billion units", 3),
    (r"\bcrore (units|kwh)\b", "MU", 0.1, "crore units", 3),
    (r"\blakh (units|kwh)\b", "MU", 10.0, "lakh units", 2),
    (r"\bmwh\b|\bmegawatt[ -]?hours?\b", "MU", 1000.0, "MWh", 0),
    (r"\bkwh\b|\bkilowatt[ -]?hours?\b", "MU", 1e6, "kWh", 0),
    (r"\bmillion units\b|\bmillion kwh\b", "MU", 1.0, "million units", 3),
    (r"\bgw\b|\bgigawatts?\b", "MW", 0.001, "GW", 3),
    (r"\bkw\b|\bkilowatts?\b", "MW", 1000.0, "kW", 0),
    (r"\blakh (tonnes|tons|t|mt)\b", "tonnes", 1e-5, "lakh tonnes", 2),
    (r"\bmillion (tonnes|tons|t)\b|\bmmt\b", "tonnes", 1e-6, "million tonnes", 3),
    (r"\bthousand (tonnes|tons)\b|\bkilo ?tonnes\b", "tonnes", 1e-3, "thousand tonnes", 1),
    (r"\btmc(ft)?\b|\bthousand million cubic feet\b", "MCFT", 1e-3, "TMC", 3),
    (r"\b(million cubic met(re|er)s|mcm)\b", "MCFT", 0.0283168, "million cubic metres", 2),
    (r"\b(in )?(met(re|er)s)\b", "feet", 0.3048, "metres", 2),
    (r"\b(in )?(feet|ft)\b", "metres", 1 / 0.3048, "feet", 2),
]
_UNIT_WORD = {"MU": r"MU\b", "MW": r"MW\b", "tonnes": r"(?:tonnes|t)\b(?![/a-z])", "MCFT": r"MCFT\b",
              "feet": r"feet\b|ft\b", "metres": r"metres\b|m\b(?![/a-z²³])"}


def requested_unit(question: str) -> tuple[str, str] | None:
    """(unit the data is kept in, unit asked for) when the question asks for figures in another unit, else None."""
    q = (question or "").lower()
    for phrase, base, _factor, label, _places in _CONVERSIONS:
        if re.search(phrase, q):
            return base, label
    return None


def convert_units(question: str, text: str) -> str:
    q = (question or "").lower()
    for phrase, base, factor, label, places in _CONVERSIONS:
        if not re.search(phrase, q):
            continue
        rx = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(" + _UNIT_WORD[base] + r")(?!\s*\()")

        def add(m: re.Match) -> str:
            try:
                v = float(m.group(1).replace(",", "")) * factor
            except ValueError:
                return m.group(0)
            return f"{m.group(0)} ({fmt_number(v, places)} {label})"
        new = rx.sub(add, text)
        return new
    return text


_COMBINE_RE = re.compile(r"\b(add up|added up|add together|total of|combined|together|sum of|in all|altogether|put together)\b",
                         re.IGNORECASE)


def combined_total(question: str, columns: list[str], rows: list[tuple]) -> str:
    """'Add up the generation of RTPS and BTPS': a sentence with the sum of each additive measure, else ''."""
    if len(rows) < 2 or not _COMBINE_RE.search(question or ""):
        return ""
    parts = []
    for i, c in enumerate(columns):
        vals = [to_float(r[i]) for r in rows]
        if any(v is None for v in vals) or not col_unit(c) or not is_additive(c):
            continue
        parts.append(f"{header(c)}: {fmt_value(c, sum(vals))}")
    return ("Together: " + "; ".join(parts) + ".") if parts else ""
