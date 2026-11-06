"""
Deterministic single-figure answers from the metric registry (no LLM).

Handles a question that names exactly ONE metric, at most ONE scope (a unit, a station, a plant category / type,
or KPCL as a whole) and exactly ONE period (a date, month, quarter of an FY, FY, water year, or season of an FY),
with no ranking / comparison / breakdown / trend wording. Example: "What was RTPS's PLF in March 2025?".
Anything else returns None and moves on to the LLM path.

The SQL is assembled from reviewed registry expressions and fixed filters; the only values placed in it are
bound parameters taken from validated entities (station and unit keys, category labels, period values), never
user text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answer import fmt_value
from app.db import ReadOnlyDB
from app.semantic_layer import load_semantic_layer, router_index

UNIT_DAILY = "mart_fact_unit_generation_daily"
UNIT_MONTHLY = "mart_fact_unit_generation_monthly"
STATION_MONTHLY = "mart_fact_station_generation_monthly"
STATION_FY = "mart_fact_station_generation_fy"
_EXPECTED_SCOPE = {"date": "document_day", "month": "document_month_total", "fy": "document_fy_total"}
_ANCHOR_METRICS = ("gross_generation", "plf")


@dataclass
class MetricAnswer:
    answer: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    metric: str = ""


def _metric(key: str) -> dict:
    return next(m for m in load_semantic_layer()["metrics"] if m["key"] == key)


def _scope(entities: dict, metric: dict | None = None) -> tuple[str, str | None, str] | None:
    """(kind, value, display) -- kind is reservoir / unit / station / category / plant_type / company."""
    units, stations, cats = entities.get("units", []), entities.get("stations", []), entities.get("categories", [])
    reservoirs = entities.get("reservoirs", [])
    if metric and metric.get("reservoir_only"):
        if len(reservoirs) == 1 and not units:
            return "reservoir", reservoirs[0], reservoirs[0].title()
        return None
    if reservoirs:
        return None
    # State-grid measures belong to the state, not to a station: with no place named, the scope is the state.
    if metric and metric.get("company_only") and not (units or stations or cats or entities.get("plant_type")):
        return "company", None, "Karnataka"
    if len(units) == 1 and len(stations) <= 1:
        return "unit", units[0], units[0]
    if units:
        return None
    if len(stations) == 1 and not cats:
        return "station", stations[0], stations[0]
    if stations:
        return None
    if len(cats) == 1:
        label = cats[0]
        return "category", label, {"THERMAL-COAL": "thermal (coal) stations", "RES": "the waste-to-energy plant"}.get(
            label, f"{label.lower()} stations")
    if cats:
        return None
    if entities.get("plant_type"):
        return "plant_type", entities["plant_type"], f"{entities['plant_type'].lower()} stations"
    # KPCL-wide only when the question says so ("KPCL", "total", "all stations"); an unnamed or unrecognised
    # place must never be answered with the company total.
    return ("company", None, "KPCL") if entities.get("scope") == "company" else None


def _table_for(metric: dict, scope_kind: str, period: dict) -> tuple[str, str] | None:
    """(table, expression family 'daily' | 'rollup' | 'all')."""
    kind = period["kind"]
    if metric.get("source_table"):
        return metric["source_table"], "all"
    if kind in ("date", "water_year", "season", "calendar_year", "season_cy"):
        if metric.get("rollup_only"):
            return None
        return UNIT_DAILY, "daily"
    if scope_kind == "unit":
        return UNIT_MONTHLY, "rollup"
    if kind == "fy":
        return STATION_FY, "rollup"
    return STATION_MONTHLY, "rollup"


def _expr(metric: dict, family: str) -> dict | None:
    e = metric["expr"]
    return e.get(family) or e.get("all") or (e.get("daily") if family == "all" else None)


def _period_filters(table: str, period: dict) -> tuple[list[str], list] | None:
    kind, value, fy = period["kind"], period["value"], period.get("fy")
    if kind == "date":
        return ["date_key = ?"], [value]
    if kind == "month":
        return ["month_label = ?"], [value]
    if kind == "fy":
        return ["fy = ?"], [value]
    if kind == "quarter":
        return (["fy = ?", "fy_quarter = ?"], [fy, value]) if fy else None
    if kind == "water_year":
        return (["water_year = ?"], [value]) if table != "mart_fact_coal_receipt" else None
    if kind == "season":
        return (["fy = ?", "season = ?"], [fy, value]) if fy else None
    if kind == "calendar_year":
        return ["date_key BETWEEN ? AND ?"], [f"{value}-01-01", f"{value}-12-31"]
    if kind == "season_cy":
        return ["season = ?", "date_key BETWEEN ? AND ?"], [value, f"{fy}-01-01", f"{fy}-12-31"]
    return None


_SCOPE_COL = {"unit": "unit_key", "station": "station_key", "category": "plant_category_label", "plant_type": "plant_type_code",
              "reservoir": "reservoir_name"}
_TABLE_HAS = {  # which scope columns each non-generation source table carries
    "mart_fact_generation_loss_daily": {"unit", "station", "category"},
    "mart_fact_coal_stock_daily": {"station"}, "mart_fact_coal_receipt": {"station"},
    "mart_fact_unit_outage": {"unit", "station", "category", "plant_type"}, "mart_fact_grid_daily": set(),
    "mart_fact_reservoir_daily": {"reservoir"},
}
# Wording this lookup cannot honour exactly; such questions go to the model instead of getting a plain total.
_DECLINE_RE = re.compile(r"\b(average|avg|mean|per day|a day|daily average|per unit|per month|per year|median|"
                         r"due to|because of|owing to|reasons?|lose|lost|losing|loss(es)? (to|from|due))\b")


def _period_phrase(period: dict) -> str:
    kind, label = period["kind"], period["label"]
    if kind == "date":
        return f"on {label}"
    return f"in {label}"


def _as_of_period(entities: dict) -> dict | None:
    """'now' / 'currently' with no period named: the last day in the data."""
    d = entities.get("as_of")
    return {"kind": "date", "value": d, "fy": None, "label": f"{d[8:10]}-{_MON[int(d[5:7]) - 1]}-{d[:4]}",
            "relative": "now"} if d else None


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _show(metric: dict, value: float, level_unit: str | None = None) -> str:
    unit = metric["unit"]
    if unit == "days":
        return f"{int(value)} days"
    if unit == "level":
        return f"{value:,.2f} {'feet' if (level_unit or '').upper() == 'FT' else 'metres'}"
    if unit in ("MW", "Hz", "mm"):
        return f"{value:,.{metric['precision']}f} {unit}"
    col_hint = {"%": "x_pct", "MU": "x_mu", "tonnes": "x_mt", "kL": "x_kl", "hours": "x_hrs", "kcal/kWh": "x_kcal_kwh",
                "kcal/kg": "x_kcal_kg", "kg/kWh": "x_kg_kwh", "ml/kWh": "x_ml_kwh", "MCFT": "x_mcft", "count": "n_count",
                "days": "n_days"}.get(unit, "x")
    return fmt_value(col_hint, value)


def _date_period(d: str) -> dict:
    return {"kind": "date", "value": d, "fy": None, "label": f"{d[8:10]}-{_MON[int(d[5:7]) - 1]}-{d[:4]}", "relative": None}


async def _level_over_period(entities: dict, metric: dict, period: dict, db: ReadOnlyDB, timeout_seconds: float,
                             question: str) -> MetricAnswer | None:
    """A level over a month / FY / water year: the closing position (last day with a value in the period), plus the
    period's low, high and average, so the figure is never an unexplained average of daily levels."""
    table = metric.get("source_table")
    expr = metric["expr"].get("all") or metric["expr"].get("daily")
    scope = _scope(entities, metric)
    if not table or not expr or "den" in expr or not re.fullmatch(r"[a-z_]+", expr["num"]) or scope is None:
        return None
    scope_kind, scope_value, _ = scope
    if scope_kind == "company" and table != "mart_fact_grid_daily" or \
            scope_kind != "company" and scope_kind not in _TABLE_HAS.get(table, set()):
        return None
    pf = _period_filters(table, period)
    if pf is None:
        return None
    col = expr["num"]
    where, params = list(pf[0]) + [f"{col} IS NOT NULL"], list(pf[1])
    if scope_kind != "company":
        where.append(f"{_SCOPE_COL[scope_kind]} = ?")
        params.append(scope_value)
    if metric.get("filter"):
        where.append(metric["filter"])
    p = metric["precision"]
    unit_sql = ", MAX(level_unit)" if table == "mart_fact_reservoir_daily" else ", NULL"
    sql = (f"SELECT MAX(date_key), ROUND(MIN({col}), {p}), ROUND(MAX({col}), {p}), ROUND(AVG({col}), {p}), COUNT(*)"
           f"{unit_sql} FROM {table} WHERE " + " AND ".join(where))
    res = await db.run(sql, max_rows=1, timeout_seconds=timeout_seconds, params=tuple(params))
    last, lo, hi, avg, n, level_unit = res.rows[0]
    if not n:
        return None
    closing = _date_period(last)
    base = await answer({**entities, "periods": [closing]}, db, timeout_seconds, question)
    if base is None:
        return None
    head, _, rest = base.answer.partition("\n\n")
    head = head.replace(f" on {closing['label']}", f" at the end of {period['label']} ({closing['label']})", 1)
    rng = (f"During {period['label']} it ranged from {_show(metric, lo, level_unit)} to {_show(metric, hi, level_unit)}, "
           f"averaging {_show(metric, avg, level_unit)} over {n} day{'s' if n != 1 else ''}. "
           f"{metric['label'][:1].upper() + metric['label'][1:]} is a position on one day, so a period is reported by its "
           f"closing figure.")
    return MetricAnswer(answer="\n\n".join(x for x in (head, rng, rest) if x),
                        sql=sql.replace("?", "{}").format(*[repr(x) for x in params]) + ";\n" + base.sql,
                        columns=["closing_date", "closing_value", "min", "max", "avg", "days"],
                        rows=[(last, base.rows[0][0] if base.rows else None, lo, hi, avg, n)], metric=metric["key"])


def eligible(entities: dict, question: str = "") -> bool:
    intents = set(entities.get("intents", []))
    periods = [p for p in entities.get("periods", [])]
    if not periods and entities.get("as_of") and len(entities.get("metrics", [])) == 1:
        metric = _metric(entities["metrics"][0])
        periods = [_as_of_period(entities)] if metric.get("kind") == "level" else []
    if question and _DECLINE_RE.search(question.lower()):
        return False
    if entities.get("suppliers") or entities.get("reasons"):
        return False  # this lookup cannot filter by coal supplier or outage reason; a total would be wrong
    return (len(entities.get("metrics", [])) == 1 and len(periods) == 1 and periods[0]["kind"] != "out_of_range"
            and not intents & {"ranking", "comparison", "breakdown", "trend"})


async def answer(entities: dict, db: ReadOnlyDB, timeout_seconds: float, question: str = "") -> MetricAnswer | None:
    if not eligible(entities, question):
        return None
    metric = _metric(entities["metrics"][0])
    period = entities["periods"][0] if entities.get("periods") else _as_of_period(entities)
    # A level (reservoir level, storage, energy potential) is a value on one day, never a total over a period:
    # over a month or FY it is reported as the closing position, with the period's range alongside.
    if metric.get("kind") == "level" and period["kind"] != "date":
        return await _level_over_period(entities, metric, period, db, timeout_seconds, question)
    scope = _scope(entities, metric)
    if scope is None:
        return None
    if metric.get("reservoir_only") and scope[0] != "reservoir":
        return None
    scope_kind, scope_value, scope_display = scope
    idx = router_index()

    # A thermal-only measure asked of a hydro / solar / wind station: say so rather than print "no data".
    applies = metric.get("applies_to")
    if applies and scope_kind in ("station", "unit"):
        st = scope_value.rsplit("-U", 1)[0] if scope_kind == "unit" else scope_value
        ptype = idx.station_type.get(st)
        if ptype and ptype not in applies:
            return MetricAnswer(answer=f"{metric['label'][:1].upper() + metric['label'][1:]} applies only to "
                                       f"{' and '.join(a.lower() for a in applies)} stations; {st} is a "
                                       f"{ptype.lower()} station, so there is no such figure for it.", sql="",
                                metric=metric["key"])
    if metric.get("company_only") and scope_kind != "company":
        return None
    if metric.get("station_only") and scope_kind not in ("station", "company"):
        return None

    chosen = _table_for(metric, scope_kind, period)
    if chosen is None:
        return None
    table, family = chosen
    expr = _expr(metric, family)
    if expr is None:
        return None
    pf = _period_filters(table, period)
    if pf is None:
        return None
    where, params = list(pf[0]), list(pf[1])
    if scope_kind != "company":
        if table in _TABLE_HAS and scope_kind not in _TABLE_HAS[table]:
            return None
        where.append(f"{_SCOPE_COL[scope_kind]} = ?")
        params.append(scope_value)
    if metric.get("filter"):
        where.append(metric["filter"])
    num = expr["num"]
    if "den" in expr:
        value_sql = f"ROUND(({num}) * {expr.get('scale', 1)} / NULLIF({expr['den']}, 0), {metric['precision']})"
    else:
        value_sql = f"ROUND({num}, {metric['precision']})" if metric["kind"] != "count" else num
    # The report pins a figure only at its own grain: a day, a month or an FY. Every row in scope must carry it.
    expected = _EXPECTED_SCOPE.get(period["kind"])
    anchor_sql = (f", SUM(CASE WHEN anchor_scope = '{expected}' THEN 1 ELSE 0 END) AS doc_rows"
                  if expected and table in (UNIT_DAILY, UNIT_MONTHLY, STATION_MONTHLY, STATION_FY) else ", 0 AS doc_rows")
    level_unit_sql = ", MAX(level_unit) AS level_unit" if table == "mart_fact_reservoir_daily" else ", NULL AS level_unit"
    sql = (f"SELECT {value_sql} AS value{anchor_sql}, COUNT(*) AS n_rows{level_unit_sql} FROM {table} WHERE "
           + " AND ".join(where))
    res = await db.run(sql, max_rows=1, timeout_seconds=timeout_seconds, params=tuple(params))
    value, doc_rows, n_rows, level_unit = res.rows[0]

    unit = metric["unit"]
    who = "KPCL's total" if scope_kind == "company" else scope_display
    label = metric["label"]
    phrase = _period_phrase(period)
    if not n_rows or value is None:
        where_txt = {"company": "Karnataka" if metric.get("subject") else "KPCL"}.get(scope_kind, scope_display)
        text = (f"There is no {label} figure for {where_txt} {phrase} in the data"
                f"{' (the unit or station may not have been commissioned yet)' if scope_kind in ('unit', 'station') else ''}.")
        return MetricAnswer(answer=text, sql=sql, columns=["value"], rows=[(None,)], metric=metric["key"])
    shown = _show(metric, value, level_unit)
    if scope_kind == "reservoir":
        sentence = f"The {label} of {who} reservoir {phrase} was {shown}."
    elif scope_kind in ("unit", "station"):
        sentence = f"{who} {label} {phrase} was {shown}."
    elif scope_kind in ("category", "plant_type"):
        sentence = f"The {label} of {who} {phrase} was {shown}."
    elif metric.get("subject"):
        sentence = f"{metric['subject']} {label} {phrase} was {shown}."
    else:
        subject = label[:1].upper() + label[1:] if label.lower().startswith("kpcl") else f"KPCL's {label}"
        sentence = f"{subject} {phrase} was {shown}" + (" across all stations." if metric["kind"] != "ratio" else ".")
    notes = []
    if period.get("relative") == "now":
        notes.append("'Now' means the last day in the data, 30-Apr-2025.")
    elif period.get("relative"):
        notes.append(f"'{period['relative']}' is read against the end of the data, 30-Apr-2025.")
    if period["kind"] == "fy" and period["value"] == "2025-26" or period["kind"] == "water_year" and period["value"] == "2024-25":
        notes.append("The data ends on 30-Apr-2025, so this period is incomplete.")
    if period["kind"] in ("calendar_year", "season_cy"):
        y = period["value"] if period["kind"] == "calendar_year" else period["fy"]
        if y == "2015":
            notes.append("The data starts on 01-Apr-2015, so January to March 2015 are not included.")
        elif y == "2025":
            notes.append("The data ends on 30-Apr-2025, so only January to April 2025 are included.")
        elif period["kind"] == "calendar_year":
            notes.append("This is the calendar year, January to December; ask for 'FY' for the April-March financial year.")
    if metric["key"] in _ANCHOR_METRICS and n_rows and doc_rows == n_rows:
        notes.append("This figure is pinned to the K-eRP report for that period.")
    if metric["key"] == "running_hours" and scope_kind != "unit":
        notes.append("Running hours are unit-hours, summed over the units in scope.")
    text = f"**{sentence[:1].upper() + sentence[1:]}**" + ("\n\n" + " ".join(notes) if notes else "")
    return MetricAnswer(answer=text, sql=sql.replace("?", "{}").format(*[repr(p) for p in params]),
                        columns=["value"], rows=[(value,)], metric=metric["key"])
