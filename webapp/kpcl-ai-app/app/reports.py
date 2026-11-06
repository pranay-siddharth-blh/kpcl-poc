"""
Reviewed, parameterised reports for broad client-style questions (the WB POC's TEST_QUESTIONS_2 style:
"Show me the performance of all thermal stations in FY 2023-24", "Which stations need attention first?",
"Provide me the units that are at risk", "Show me the coal situation across all thermal stations").

Each report is fixed SQL written and checked once (read-only, mart_* tables, bound parameters taken from validated
entities only), plus a deterministic writer. A report matches only on explicit wording AND the right entities, so a
single-figure question never lands here. Parameters: the FY named in the question (default: the latest complete FY),
the plant category or type named, one station where the report supports it. Anything else falls through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import dashboard_data
from app.answer import build_chart, fmt_value
from app.db import ReadOnlyDB
from app.semantic_layer import norm

LATEST_COMPLETE_FY = "2024-25"
_CAT_LABEL = {"THERMAL-COAL": "thermal (coal)", "MAJOR HYDRO": "major hydro", "MINOR HYDRO": "minor hydro", "GAS": "gas",
              "RES": "waste-to-energy", "SOLAR": "solar", "WIND": "wind"}


@dataclass
class Report:
    key: str
    answer: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    chart: dict | None = None
    total: tuple | None = None    # a total row for the Excel report, from the year's own figures (breakdowns)


def _v(col: str, v) -> str:
    return fmt_value(col, v)


def _b(text: str) -> str:
    return f"**{text}**"


def _fy(entities: dict) -> str | None:
    """The one FY named, the latest complete FY when none is named, None when another kind of period is named."""
    ps = entities.get("periods", [])
    if not ps:
        return LATEST_COMPLETE_FY
    fys = [p["value"] for p in ps if p["kind"] == "fy"]
    return fys[0] if len(fys) == 1 and len(ps) == 1 else None


def _prev_fy(fy: str) -> str:
    a = int(fy[:4]) - 1
    return f"{a}-{(a + 1) % 100:02d}"


def _scope_filter(entities: dict, alias: str = "") -> tuple[str, list, str]:
    """(SQL condition, params, words) for a named category or plant type; ('1 = 1', [], 'all') otherwise."""
    p = f"{alias}." if alias else ""
    cats = entities.get("categories", [])
    if len(cats) == 1:
        return f"{p}plant_category_label = ?", [cats[0]], _CAT_LABEL.get(cats[0], cats[0].lower())
    if entities.get("plant_type"):
        return f"{p}plant_type_code = ?", [entities["plant_type"]], entities["plant_type"].lower()
    return "1 = 1", [], "all"


async def _q(db: ReadOnlyDB, sql: str, params: list, timeout: float, max_rows: int = 200):
    return await db.run(sql, max_rows=max_rows, timeout_seconds=timeout, restricted=True, params=tuple(params))


def _dicts(res) -> list[dict]:
    return [dict(zip(res.columns, r)) for r in res.rows]


def _lines(items: list[str]) -> str:
    return "\n".join(f"•  {x}" for x in items)


# ---------------------------------------------------------------------------------------------- 1. station scorecard
_PERF_RE = re.compile(r"\b(performance|perform\w*|scorecard|report card|side by side|league table|rank (all|the|every)|"
                      r"how (did|have|were|are) (the |all |our )?(\w+ )?(stations|plants)|compare all|all (the )?(stations|plants))\b")
_PLURAL_RE = re.compile(r"\b(stations|plants|all|every|each|across|categories|category)\b")

PERF_SQL = """
SELECT f.station_key AS station, s.station_name, f.plant_category_label AS category, s.installed_capacity_mw AS capacity_mw,
       ROUND(f.gross_gen_mu, 3) AS gross_gen_mu,
       ROUND(f.gross_gen_mu * 100.0 / NULLIF(f.capacity_mu, 0), 2) AS plf_pct,
       ROUND(f.gross_gen_mu * 100.0 / NULLIF(f.cea_target_mu, 0), 2) AS cea_target_achievement_pct,
       ROUND((f.gross_gen_mu - p.gross_gen_mu) * 100.0 / NULLIF(p.gross_gen_mu, 0), 2) AS yoy_growth_pct
FROM mart_fact_station_generation_fy f
JOIN mart_dim_station s ON s.station_key = f.station_key
LEFT JOIN mart_fact_station_generation_fy p ON p.station_key = f.station_key AND p.fy = ?
WHERE f.fy = ? AND {cond}
ORDER BY {order} DESC NULLS LAST"""


async def station_scorecard(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not (_PERF_RE.search(q) and _PLURAL_RE.search(q)) or e.get("stations") or e.get("units") or e.get("reservoirs"):
        return None
    if not set(e.get("metrics", [])) <= {"gross_generation", "plf", "cea_target_achievement", "cea_target"}:
        return None
    fy = _fy(e)
    if fy is None:
        return None
    cond, params, words = _scope_filter(e, "f")
    by_plf = "plf" in e.get("metrics", []) or re.search(r"\bplf|load factor|cuf\b", q)
    sql = PERF_SQL.format(cond=cond, order="plf_pct" if by_plf else "gross_gen_mu")
    res = await _q(db, sql, [_prev_fy(fy), fy] + params, t)
    r = _dicts(res)
    if not r:
        return None
    total = sum(x["gross_gen_mu"] or 0 for x in r)
    with_plf = [x for x in r if x["plf_pct"] is not None and (x["gross_gen_mu"] or 0) > 0]
    best = max(with_plf, key=lambda x: x["plf_pct"]) if with_plf else None
    worst = min(with_plf, key=lambda x: x["plf_pct"]) if with_plf else None
    tgt = [x for x in r if x["cea_target_achievement_pct"] is not None]
    met = [x for x in tgt if x["cea_target_achievement_pct"] >= 100]
    who = "KPCL's" if words == "all" else f"the {words}"
    head = (f"In FY {fy}, {who} {len(r)} station{'s' if len(r) != 1 else ''} generated {_v('x_mu', total)}"
            + (f"; {met and len(met) or 'none'} of {len(tgt)} met their CEA target." if tgt else "."))
    body = []
    if best and worst and best is not worst:
        body.append(f"Highest PLF: {best['station']} ({_v('x_pct', best['plf_pct'])}); lowest: {worst['station']} "
                    f"({_v('x_pct', worst['plf_pct'])}).")
    top = r[:12]
    body.append(_lines([f"{x['station']} — {_v('x_mu', x['gross_gen_mu'])}, PLF {_v('x_pct', x['plf_pct'])}, "
                        f"CEA target {_v('x_pct', x['cea_target_achievement_pct'])}"
                        + (f", {x['yoy_growth_pct']:+.1f}% on FY {_prev_fy(fy)}" if x['yoy_growth_pct'] is not None else "")
                        for x in top]))
    if len(r) > len(top):
        body.append(f"The other {len(r) - len(top)} stations are in the table below.")
    chart = build_chart("bar", "station", "plf_pct" if by_plf else "gross_gen_mu",
                        "PLF (%)" if by_plf else "Gross generation (MU)", res.columns, res.rows)
    return Report("station_scorecard", "\n\n".join([_b(head)] + body), sql, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 2. attention
_ATTN_RE = re.compile(r"\b(need\w* (the most )?attention|attention first|underperform\w*|under-perform\w*|lagging|"
                      r"problem stations|stations of concern|worst[- ]performing|poor(est)? perform\w*|"
                      r"consistently (miss\w*|below|short|under\w*|fail\w*)|falling behind|biggest problems?)\b")

ATTN_SQL = """
SELECT f.station_key AS station, f.plant_category_label AS category,
       ROUND(f.gross_gen_mu * 100.0 / NULLIF(f.cea_target_mu, 0), 2) AS cea_target_achievement_pct,
       ROUND(f.gross_gen_mu * 100.0 / NULLIF(f.capacity_mu, 0), 2) AS plf_pct,
       ROUND(f.forced_outage_hrs, 1) AS forced_outage_hrs,
       (SELECT COUNT(*) FROM mart_fact_station_generation_fy h
         WHERE h.station_key = f.station_key AND h.is_partial_period = 0 AND h.cea_target_mu > 0
           AND h.gross_gen_mu < h.cea_target_mu) AS years_below_target,
       (SELECT COUNT(*) FROM mart_fact_station_generation_fy h
         WHERE h.station_key = f.station_key AND h.is_partial_period = 0 AND h.cea_target_mu > 0) AS years_with_target,
       (SELECT COUNT(*) FROM mart_fact_unit_current_status u
         WHERE u.station_key = f.station_key AND u.status <> 'RUNNING') AS units_out_now
FROM mart_fact_station_generation_fy f
WHERE f.fy = ? AND f.cea_target_mu > 0 AND {cond}
ORDER BY cea_target_achievement_pct"""


async def attention(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _ATTN_RE.search(q) or e.get("stations") or e.get("units"):
        return None
    fy = _fy(e)
    if fy is None:
        return None
    cond, params, words = _scope_filter(e, "f")
    sql = ATTN_SQL.format(cond=cond)
    res = await _q(db, sql, [fy] + params, t)
    r = _dicts(res)
    if not r:
        return None
    short = [x for x in r if (x["cea_target_achievement_pct"] or 0) < 100]
    chronic = sorted([x for x in r if x["years_below_target"] and x["years_below_target"] >= max(1, x["years_with_target"] - 1)],
                     key=lambda x: (-x["years_below_target"], x["cea_target_achievement_pct"] or 0))
    out_now = [x for x in r if x["units_out_now"]]
    head = (f"{len(short)} of {len(r)} {'' if words == 'all' else words + ' '}stations fell short of their CEA target in FY {fy}; "
            f"the furthest behind were " + ", ".join(f"{x['station']} ({_v('x_pct', x['cea_target_achievement_pct'])})"
                                                   for x in short[:3]) + "." if short else
            f"Every {'' if words == 'all' else words + ' '}station met its CEA target in FY {fy}.")
    body = []
    if chronic:
        body.append("Missed the target in almost every complete year (FY 2015-16 to FY 2024-25): "
                    + ", ".join(f"{x['station']} ({x['years_below_target']} of {x['years_with_target']} years)"
                                for x in chronic[:6]) + ".")
    if out_now:
        body.append("Units out of service as on 30-Apr-2025: "
                    + ", ".join(f"{x['station']} ({x['units_out_now']})" for x in out_now) + ".")
    body.append(_lines([f"{x['station']} — CEA target {_v('x_pct', x['cea_target_achievement_pct'])}, PLF "
                        f"{_v('x_pct', x['plf_pct'])}, forced outage {_v('x_hrs', x['forced_outage_hrs'])}" for x in r[:8]]))
    chart = build_chart("bar", "station", "cea_target_achievement_pct", "CEA target achievement (%)", res.columns, res.rows[:15])
    return Report("attention", "\n\n".join([_b(head)] + body), sql, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 3. units at risk
_RISK_RE = re.compile(r"\bunits?\b[^.?!]{0,40}\b(at[- ]risk|risky|of concern|vulnerable|unreliable|problem\w*|trouble\w*)\b|"
                      r"\b(at[- ]risk|risky|unreliable|problem|vulnerable) units?\b")

RISK_SQL = """
SELECT m.unit_key AS unit, m.station_key AS station, ROUND(SUM(m.forced_outage_hrs), 1) AS forced_outage_hrs,
       SUM(m.trips) AS trips, ROUND(SUM(m.gross_gen_mu) * 100.0 / NULLIF(SUM(m.capacity_mu), 0), 2) AS plf_pct,
       MAX(s.status) AS status_now, MAX(s.days_in_status) AS days_in_status
FROM mart_fact_unit_generation_monthly m
LEFT JOIN mart_fact_unit_current_status s ON s.unit_key = m.unit_key
WHERE m.fy = ? AND {cond}
GROUP BY m.unit_key, m.station_key
ORDER BY (MAX(s.status) <> 'RUNNING') DESC, SUM(m.forced_outage_hrs) DESC
LIMIT 12"""


async def units_at_risk(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _RISK_RE.search(q) or e.get("units"):
        return None
    fy = _fy(e)
    if fy is None:
        return None
    if e.get("stations"):
        if len(e["stations"]) != 1:
            return None
        cond, params, words = "m.station_key = ?", [e["stations"][0]], e["stations"][0]
    else:
        cond, params, words = _scope_filter(e, "m")
    sql = RISK_SQL.format(cond=cond)
    res = await _q(db, sql, [fy] + params, t)
    r = _dicts(res)
    if not r:
        return None
    out = [x for x in r if x["status_now"] and x["status_now"] != "RUNNING"]
    worst = [x for x in r if x not in out][:5]
    head = (f"{len(out)} unit{'s are' if len(out) != 1 else ' is'} out of service now (30-Apr-2025)" if out
            else "No unit in scope is out of service now (30-Apr-2025)") + (
        f"; the least reliable of the rest in FY {fy}: " + ", ".join(
            f"{x['unit']} ({_v('x_hrs', x['forced_outage_hrs'])} forced outage, {x['trips']} trips)" for x in worst[:3]) + "."
        if worst else ".")
    body = []
    if out:
        body.append(_lines([f"{x['unit']} — {x['status_now'].replace('_', ' ').lower()}, {int(x['days_in_status'] or 0)} days"
                            for x in out]))
    body.append("Risk here means: out of service now, or the most forced-outage hours and trips in the year. "
                "The data has no condition-monitoring or maintenance records.")
    chart = build_chart("bar", "unit", "forced_outage_hrs", "Forced outage hours", res.columns, res.rows)
    return Report("units_at_risk", "\n\n".join([_b(head)] + body), sql, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 4. coal situation
_COAL_RE = re.compile(r"\bcoal\b[^.?!]{0,40}\b(situation|position|status|picture|overview|across|all thermal|each thermal|"
                      r"every thermal|stations?)\b|\b(situation|position|status|picture) of coal\b")

COAL_SQL = """
SELECT c.station_key AS station, ROUND(c.closing_stock_mt, 0) AS closing_stock_mt, ROUND(c.days_of_stock, 1) AS days_of_stock,
       c.normative_days, c.is_below_norm,
       (SELECT SUM(d.is_below_norm) FROM mart_fact_coal_stock_daily d WHERE d.station_key = c.station_key AND d.fy = ?) AS days_below_norm_in_fy,
       (SELECT ROUND(SUM(r.quantity_mt), 0) FROM mart_fact_coal_receipt r WHERE r.station_key = c.station_key AND r.fy = ?) AS receipt_mt_in_fy
FROM mart_fact_coal_stock_daily c
WHERE c.date_key = (SELECT MAX(date_key) FROM mart_fact_coal_stock_daily)
ORDER BY c.days_of_stock"""


async def coal_situation(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _COAL_RE.search(q) or e.get("stations") or e.get("metrics") and not set(e["metrics"]) <= {"coal_receipt", "coal_consumption"}:
        return None
    fy = _fy(e)
    if fy is None:
        return None
    res = await _q(db, COAL_SQL, [fy, fy], t)
    r = _dicts(res)
    below = [x for x in r if x["is_below_norm"]]
    head = (f"As on 30-Apr-2025, {len(below)} of {len(r)} thermal stations were below their normative coal stock; "
            f"lowest {r[0]['station']} at {r[0]['days_of_stock']} days against a {r[0]['normative_days']}-day norm.")
    body = [_lines([f"{x['station']} — {_v('x_mt', x['closing_stock_mt'])} in stock, {x['days_of_stock']} days "
                    f"(norm {x['normative_days']}); below norm on {x['days_below_norm_in_fy']} days in FY {fy}; "
                    f"received {_v('x_mt', x['receipt_mt_in_fy'])} in FY {fy}" for x in r])]
    chart = build_chart("bar", "station", "days_of_stock", "Days of coal stock (30-Apr-2025)", res.columns, res.rows)
    return Report("coal_situation", "\n\n".join([_b(head)] + body), COAL_SQL, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 5. reservoir position
_RES_RE = re.compile(r"\b(reservoirs?|dams?)\b[^.?!]{0,50}\b(position|situation|status|picture|overview|across|all|every|each|"
                     r"levels|lowest|highest|fullest|emptiest|most full|least full)\b|"
                     r"\b(position|situation|status|levels?) (of|across|in) (all |the |every |each )?(reservoirs?|dams?)\b|"
                     r"\b(lowest|highest|fullest|emptiest) (reservoir|dam)s?\b")

RES_SQL = """
SELECT reservoir_name AS reservoir, ROUND(water_level, 2) AS water_level, level_unit, full_reservoir_level,
       min_draw_down_level, ROUND(capacity_pct, 2) AS capacity_pct, ROUND(energy_potential_mu, 2) AS energy_potential_mu,
       feeds_stations
FROM mart_fact_reservoir_daily
WHERE date_key = (SELECT MAX(date_key) FROM mart_fact_reservoir_daily)
ORDER BY capacity_pct DESC"""


async def reservoir_position(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    now_only = all(p["kind"] == "date" and p["value"] == "2025-04-30" for p in e.get("periods", []))
    if not _RES_RE.search(q) or len(e.get("reservoirs", [])) == 1 or not now_only:
        return None
    res = await _q(db, RES_SQL, [], t)
    r = _dicts(res)
    lo, hi = r[-1], r[0]
    unit = lambda x: "ft" if (x["level_unit"] or "").upper() == "FT" else "m"  # noqa: E731
    head = (f"As on 30-Apr-2025, {hi['reservoir'].title()} was the fullest reservoir at {_v('x_pct', hi['capacity_pct'])} "
            f"and {lo['reservoir'].title()} the lowest at {_v('x_pct', lo['capacity_pct'])}.")
    body = [_lines([f"{x['reservoir'].title()} — {_v('x_pct', x['capacity_pct'])} full, level {x['water_level']:,.2f} {unit(x)} "
                    f"(FRL {x['full_reservoir_level']:,.2f}, MDDL {x['min_draw_down_level']:,.2f} {unit(x)}); feeds {x['feeds_stations']}"
                    for x in r])]
    chart = build_chart("bar", "reservoir", "capacity_pct", "Storage (% of capacity)", res.columns, res.rows)
    return Report("reservoir_position", "\n\n".join([_b(head)] + body), RES_SQL, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 6. FY summary (dashboard takeaways)
_SUMMARY_RE = re.compile(r"\b(summary|summari[sz]e|overview|highlights|key (facts|figures|numbers|findings)|at a glance|"
                         r"how did kpcl (do|perform)|kpcl'?s? (overall )?performance|performance of kpcl|big picture|"
                         r"brief me|state of (kpcl|generation)|update (me|us) on|report card)\b")
# With exactly one station named, "performance" / "how did X do" asks for that station's summary.
_STATION_PERF_RE = re.compile(r"\b(performance|perform(ed|ing)?|how (did|is|was|has) [\w-]+ (do|doing|done|been doing))\b")
_ACTION_RE = re.compile(r"\b(top|key|main|biggest)\s+((five|5|three|3|ten|10|few)\s+)?(things|priorities|actions|issues|concerns|"
                        r"risks|problems)\b|\bwhat should (management|kpcl|we|the government|the board)\b|\bact on\b|"
                        r"\bpriorit(y|ies|i[sz]e)\b")


async def fy_summary(q: str, e: dict, db: ReadOnlyDB, t: float, force: bool = False) -> Report | None:
    actions = bool(_ACTION_RE.search(q))
    one_station_perf = len(e.get("stations", [])) == 1 and bool(_STATION_PERF_RE.search(q)) and not e.get("metrics")
    if not (force or _SUMMARY_RE.search(q) or actions or one_station_perf) or e.get("units") or len(e.get("stations", [])) > 1 \
            or e.get("categories") or PROFILE_TOPIC.search(q):
        return None
    fy = _fy(e)
    if fy is None:
        return None
    station = e["stations"][0] if e.get("stations") else None
    d = await dashboard_data.build_dashboard(db, fy, station, t)
    hl = d["highlights"]
    if actions:
        hl = [h for h in hl if h["tone"] == "bad"] + [h for h in hl if h["tone"] != "bad"]
        head = f"What stands out for action, from FY {fy} and the position on 30-Apr-2025:"
    else:
        head = f"{station or 'KPCL'} in FY {fy}{' (so far)' if d['is_partial'] else ''} at a glance:"
    body = "\n".join(f"•  {h['text']}" for h in hl)
    k = d["kpis"]["cur"]
    cols = ["measure", "value"]
    rows = [("Gross generation (MU)", round(k["gen"] or 0, 3)), ("PLF (%)", k["plf"]), ("CEA target achievement (%)", k["achievement"]),
            ("Auxiliary consumption (%)", k["aux"]), ("Forced outage hours", round(k["forced_hrs"] or 0, 1)), ("Trips", k["trips"])]
    return Report("fy_summary", f"{_b(head)}\n\n{body}", "-- dashboard summary queries (app/dashboard_data.py)", cols, rows)


# ---------------------------------------------------------------------------------------------- 6b. what happened in a period
# A question that names a period and nothing else ("What was in March 2024?", "What happened in FY 2023-24?",
# "March 2024", "How was March 2024 for RTPS?") gets an overview of that month (or the FY summary above).
_VAGUE_WORDS = set("""what whats what's was were is are happened happen happening in during for of the about tell me us
show give how did do does it go went kpcl kpcl's overall summary overview recap brief report like look looked picture status
performance position things anything everything all key highlights a an on please can you could update s month year fy
financial at whole entire with mis weekly daily monthly wise detailed complete full comprehensive excel
generate prepare make download reports""".split())


def _only_names_a_period(q: str, e: dict) -> bool:
    ps = e.get("periods", [])
    if len(ps) != 1 or ps[0]["kind"] not in ("month", "fy") or e.get("metrics") or e.get("units") \
            or len(e.get("stations", [])) > 1 or e.get("categories"):
        return False
    rest = re.sub(r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sept?(ember)?|oct(ober)?|"
                  r"nov(ember)?|dec(ember)?)\b|\b(19|20)?\d\d\b|[^a-z' ]", " ", q)
    stations = {s.lower() for s in e.get("stations", [])}
    words = [w for w in rest.split() if w not in _VAGUE_WORDS and w not in stations]
    if words and e.get("stations"):   # "raichur thermal" style aliases: drop the words of a recognised station name
        from app.semantic_layer import load_semantic_layer
        alias_words = {w for a in load_semantic_layer()["entities"]["station_aliases"].get(e["stations"][0], []) for w in a.split()}
        words = [w for w in words if w not in alias_words]
    return not words


MONTH_OVERVIEW_SQL = """
SELECT m.station_key AS station, m.plant_category_label AS category, ROUND(m.gross_gen_mu, 3) AS gross_gen_mu,
       ROUND(m.cea_target_mu, 3) AS cea_target_mu,
       ROUND(m.gross_gen_mu * 100.0 / NULLIF(m.cea_target_mu, 0), 2) AS cea_target_achievement_pct,
       ROUND(m.gross_gen_mu * 100.0 / NULLIF(m.capacity_mu, 0), 2) AS plf_pct,
       ROUND(m.forced_outage_hrs, 1) AS forced_outage_hrs, m.trips,
       ROUND(p.gross_gen_mu, 3) AS prev_year_gen_mu, m.capacity_mu, m.plant_type_code
FROM mart_fact_station_generation_monthly m
LEFT JOIN mart_fact_station_generation_monthly p ON p.station_key = m.station_key AND p.year_month = ?
WHERE m.year_month = ? AND {cond}
ORDER BY m.gross_gen_mu DESC NULLS LAST"""

GRID_MONTH_SQL = """
SELECT ROUND(SUM(kpcl_gross_gen_mu) * 100.0 / NULLIF(SUM(state_consumption_mu), 0), 2) AS kpcl_share_of_consumption_pct,
       MAX(system_peak_mw) AS system_peak_mw, ROUND(SUM(state_consumption_mu), 1) AS state_consumption_mu
FROM mart_fact_grid_daily WHERE year_month = ?"""


def _pct_change(cur: float, prev: float | None) -> str:
    if not prev:
        return ""
    p = (cur - prev) * 100 / prev
    return f"{abs(p):.1f}% {'more' if p >= 0 else 'less'} than"


async def period_overview(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _only_names_a_period(q, e):
        return None
    p = e["periods"][0]
    if p["kind"] == "fy":
        return await fy_summary(q, e, db, t, force=True)
    mon, yr = p["value"].split("-")
    ym = f"{yr}-{_MONTHS.index(mon) + 1:02d}"
    prev_ym, prev_label = f"{int(yr) - 1}-{ym[5:]}", f"{p['label'].split()[0]} {int(yr) - 1}"
    station = e["stations"][0] if e.get("stations") else None
    cond, params = ("m.station_key = ?", [station]) if station else ("1 = 1", [])
    sql = MONTH_OVERVIEW_SQL.format(cond=cond)
    res = await _q(db, sql, [prev_ym, ym] + params, t)
    r = _dicts(res)
    if not r or not any(x["gross_gen_mu"] for x in r):
        return None
    gen = sum(x["gross_gen_mu"] or 0 for x in r)
    prev = sum(x["prev_year_gen_mu"] or 0 for x in r) or None
    tgt = sum(x["cea_target_mu"] or 0 for x in r)
    fo, trips = sum(x["forced_outage_hrs"] or 0 for x in r), sum(x["trips"] or 0 for x in r)
    who = station or "KPCL"
    chg = _pct_change(gen, prev)
    head = f"{p['label']} at {who}: {_v('x_mu', gen)} generated" + (f", {chg} {prev_label}." if chg else ".")
    lines = []
    if station:
        x = r[0]
        if x["plf_pct"] is not None:
            lines.append(f"PLF {_v('x_pct', x['plf_pct'])}.")
    else:
        by_cat: dict[str, float] = {}
        for x in r:
            by_cat[x["category"]] = by_cat.get(x["category"], 0) + (x["gross_gen_mu"] or 0)
        mix = sorted(by_cat.items(), key=lambda kv: -kv[1])
        lines.append("By source: " + ", ".join(f"{_CAT_LABEL.get(c, c.lower())} {_v('x_mu', v)}" for c, v in mix if v) + ".")
        th = [x for x in r if x["plant_type_code"] == "THERMAL"]
        th_cap = sum(x["capacity_mu"] or 0 for x in th)
        if th_cap:
            lines.append(f"Thermal PLF {_v('x_pct', sum(x['gross_gen_mu'] or 0 for x in th) * 100 / th_cap)}.")
        top = [x for x in r if x["gross_gen_mu"]][:3]
        lines.append("Largest generators: " + ", ".join(f"{x['station']} {_v('x_mu', x['gross_gen_mu'])}" for x in top) + ".")
    if tgt:
        missed = [x for x in r if x["cea_target_mu"] and (x["gross_gen_mu"] or 0) < x["cea_target_mu"]]
        line = f"CEA target {_v('x_mu', tgt)}, achieved {_v('x_pct', gen * 100 / tgt)}"
        if not station:
            line += f"; {len(missed)} of {sum(1 for x in r if x['cea_target_mu'])} stations with a target fell short"
        lines.append(line + ".")
    lines.append(f"Forced outage hours: {fo:,.1f}; trips: {trips:,}.")
    if not station:
        g = _dicts(await _q(db, GRID_MONTH_SQL, [ym], t))
        if g and g[0]["kpcl_share_of_consumption_pct"] is not None:
            lines.append(f"KPCL met {_v('x_pct', g[0]['kpcl_share_of_consumption_pct'])} of Karnataka's consumption "
                         f"({_v('x_mu', g[0]['state_consumption_mu'])}); the state's peak demand was {g[0]['system_peak_mw']:,.0f} MW.")
    tail = (f"Ask about any station or measure for {p['label']}, for example \"RTPS PLF in {p['label']}\" or "
            f"\"Which stations missed their CEA target in {p['label']}?\"")
    cols = ["station", "category", "gross_gen_mu", "cea_target_mu", "cea_target_achievement_pct", "plf_pct",
            "forced_outage_hrs", "trips", "prev_year_gen_mu"]
    rows = [tuple(x[c] for c in cols) for x in r]
    chart = None if station else build_chart("bar", "station", "gross_gen_mu", f"Gross generation, {p['label']} (MU)",
                                              cols, [row for row in rows if row[2]][:15])
    return Report("period_overview", "\n\n".join([_b(head), _lines(lines), tail]), sql, cols, rows, chart)


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


# ---------------------------------------------------------------------------------------------- 7. best / worst month
_MONTH_RE = re.compile(r"\b(worst|best|lowest|highest|weakest|strongest|poorest|peak)\s+(generation\s+)?month\b|"
                       r"\bmonth\b[^.?!]{0,40}\b(worst|best|lowest|highest|weakest|strongest|most|least)\b")

MONTH_SQL = """
SELECT year_month, month_label AS month, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu,
       ROUND(SUM(gross_gen_mu) * 100.0 / NULLIF(SUM(capacity_mu), 0), 2) AS plf_pct,
       ROUND(SUM(cea_target_mu), 3) AS cea_target_mu
FROM mart_fact_station_generation_monthly
WHERE fy = ? AND {cond}
GROUP BY year_month, month_label ORDER BY year_month"""


async def month_extreme(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _MONTH_RE.search(q) or e.get("units") or len(e.get("stations", [])) > 1:
        return None
    if e.get("metrics") and not set(e["metrics"]) <= {"gross_generation", "plf"}:
        return None
    fy = _fy(e)
    if fy is None:
        return None
    if e.get("stations"):
        cond, params, who = "station_key = ?", [e["stations"][0]], e["stations"][0]
    else:
        cond, params, words = _scope_filter(e)
        who = "KPCL" if words == "all" else f"the {words} stations"
    sql = MONTH_SQL.format(cond=cond)
    res = await _q(db, sql, [fy] + params, t)
    r = _dicts(res)
    if len(r) < 2:
        return None
    worst, best = min(r, key=lambda x: x["gross_gen_mu"]), max(r, key=lambda x: x["gross_gen_mu"])
    want_best = re.search(r"\b(best|highest|strongest|peak|most)\b", q) and not re.search(r"\b(worst|lowest|weakest|poorest|least)\b", q)
    pick, other = (best, worst) if want_best else (worst, best)
    head = (f"{pick['month'].title()} was {who}'s {'best' if want_best else 'weakest'} month of FY {fy}: "
            f"{_v('x_mu', pick['gross_gen_mu'])} (PLF {_v('x_pct', pick['plf_pct'])}).")
    body = (f"The {'weakest' if want_best else 'best'} was {other['month'].title()} at {_v('x_mu', other['gross_gen_mu'])}. "
            + (f"Against the CEA target, {pick['month'].title()} was {_v('x_mu', pick['gross_gen_mu'] - pick['cea_target_mu'])} "
               f"{'above' if pick['gross_gen_mu'] >= pick['cea_target_mu'] else 'short'}." if pick["cea_target_mu"] else ""))
    chart = build_chart("bar", "month", "gross_gen_mu", "Gross generation (MU)", res.columns, res.rows)
    return Report("month_extreme", f"{_b(head)}\n\n{body}", sql, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 8. share over the years
_SHARE_RE = re.compile(r"\b(contribution|share)\b[^.?!]{0,60}\b(over the years|over time|trend|each year|every year|by year|"
                       r"year by year|years)\b|\b(over the years|year by year)\b[^.?!]{0,60}\b(contribution|share)\b")

SHARE_SQL = """
SELECT fy, ROUND(SUM(kpcl_gross_gen_mu), 3) AS kpcl_gross_gen_mu, ROUND(SUM(state_consumption_mu), 3) AS state_consumption_mu,
       ROUND(SUM(kpcl_gross_gen_mu) * 100.0 / NULLIF(SUM(state_consumption_mu), 0), 2) AS kpcl_share_of_consumption_pct
FROM mart_fact_grid_daily GROUP BY fy ORDER BY fy"""


async def share_trend(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _SHARE_RE.search(q) or e.get("stations") or e.get("units") or e.get("periods"):
        return None
    res = await _q(db, SHARE_SQL, [], t)
    r = [x for x in _dicts(res) if x["fy"] != "2025-26"]
    first, last = r[0], r[-1]
    hi = max(r, key=lambda x: x["kpcl_share_of_consumption_pct"])
    head = (f"KPCL met {_v('x_pct', last['kpcl_share_of_consumption_pct'])} of Karnataka's electricity consumption in FY "
            f"{last['fy']}, against {_v('x_pct', first['kpcl_share_of_consumption_pct'])} in FY {first['fy']}.")
    body = (f"The highest share was {_v('x_pct', hi['kpcl_share_of_consumption_pct'])} in FY {hi['fy']}. KPCL generated "
            f"{_v('x_mu', last['kpcl_gross_gen_mu'])} in FY {last['fy']}, while the state consumed "
            f"{_v('x_mu', last['state_consumption_mu'])}.")
    rows = [tuple(x.values()) for x in r]
    chart = build_chart("line", "fy", "kpcl_share_of_consumption_pct", "KPCL share of state consumption (%)", res.columns, rows)
    return Report("share_trend", f"{_b(head)}\n\n{body}", SHARE_SQL, res.columns, rows, chart)


# ---------------------------------------------------------------------------------------------- 9. outage trend
_OUT_TREND_RE = re.compile(r"\b(forced )?outages?\b[^.?!]{0,40}\b(trend|over the (ten |10 |last ten |last 10 )?years|each year|"
                           r"every year|by year|year by year|over time)\b|\btrend (of|in) (forced )?outages\b")

OUT_TREND_SQL = """
SELECT fy, COUNT(*) AS forced_outages, ROUND(SUM(outage_hrs), 1) AS outage_hrs, ROUND(SUM(gen_loss_mu), 3) AS gen_loss_mu,
       SUM(is_btf) AS btf_count
FROM mart_fact_unit_outage WHERE outage_type = 'FORCED' AND {cond}
GROUP BY fy ORDER BY fy"""


async def outage_trend(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _OUT_TREND_RE.search(q) or e.get("units") or e.get("periods") or len(e.get("stations", [])) > 1:
        return None
    if e.get("stations"):
        cond, params, who = "station_key = ?", [e["stations"][0]], e["stations"][0]
    else:
        cond, params, words = _scope_filter(e)
        who = "KPCL" if words == "all" else f"the {words} stations"
    sql = OUT_TREND_SQL.format(cond=cond)
    res = await _q(db, sql, params, t)
    r = [x for x in _dicts(res) if x["fy"] != "2025-26"]
    if len(r) < 2:
        return None
    hi, lo = max(r, key=lambda x: x["forced_outages"]), min(r, key=lambda x: x["forced_outages"])
    head = (f"{who} had {r[-1]['forced_outages']:,} forced outages in FY {r[-1]['fy']} against {r[0]['forced_outages']:,} in "
            f"FY {r[0]['fy']}; the most were in FY {hi['fy']} ({hi['forced_outages']:,}), the fewest in FY {lo['fy']} "
            f"({lo['forced_outages']:,}).")
    body = _lines([f"FY {x['fy']} — {x['forced_outages']:,} forced outages, {_v('x_hrs', x['outage_hrs'])}, "
                   f"{_v('x_mu', x['gen_loss_mu'])} lost, {x['btf_count']} boiler tube failures" for x in r])
    rows = [tuple(x.values()) for x in r]
    chart = build_chart("line", "fy", "forced_outages", "Forced outages", res.columns, rows)
    return Report("outage_trend", f"{_b(head)}\n\n{body}", sql, res.columns, rows, chart)


# ---------------------------------------------------------------------------------------------- 10. hydro water-year comparison
_WY_CMP_RE = re.compile(r"\bhydro\w*\b[^.?!]{0,80}\bwater year\b[^.?!]{0,80}\b(compared|vs\.?|versus|against|year before|"
                        r"previous|last year|prior)\b")

WY_SQL = """
SELECT station_key AS station,
       ROUND(SUM(CASE WHEN water_year = ? THEN gross_gen_mu END), 3) AS this_wy_mu,
       ROUND(SUM(CASE WHEN water_year = ? THEN gross_gen_mu END), 3) AS previous_wy_mu
FROM mart_fact_unit_generation_daily
WHERE plant_type_code = 'HYDRO' AND water_year IN (?, ?)
GROUP BY station_key ORDER BY this_wy_mu DESC"""


async def hydro_wy_compare(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _WY_CMP_RE.search(q) or e.get("stations"):
        return None
    wys = [p["value"] for p in e.get("periods", []) if p["kind"] == "water_year"]
    if len(wys) != 1:
        return None
    wy = wys[0]
    prev = _prev_fy(wy)
    res = await _q(db, WY_SQL, [wy, prev, wy, prev], t)
    r = _dicts(res)
    cur, old = sum(x["this_wy_mu"] or 0 for x in r), sum(x["previous_wy_mu"] or 0 for x in r)
    head = (f"KPCL's hydro stations generated {_v('x_mu', cur)} in water year {wy}, "
            f"{abs(cur / old - 1) * 100:.1f}% {'more' if cur >= old else 'less'} than {_v('x_mu', old)} in water year {prev}.")
    note = " Water year 2024-25 runs to 30-Apr-2025 only (the end of the data), so it is one month short." if wy == "2024-25" else ""
    body = _lines([f"{x['station']} — {_v('x_mu', x['this_wy_mu'])} vs {_v('x_mu', x['previous_wy_mu'])}" for x in r[:12]])
    chart = build_chart("bar", "station", "this_wy_mu", f"Generation, water year {wy} (MU)", res.columns, res.rows)
    return Report("hydro_wy_compare", f"{_b(head)}{note}\n\n{body}", WY_SQL, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 11. thermal efficiency
_EFF_RE = re.compile(r"\b(most|least) efficient\b|\befficien\w*\b[^.?!]{0,40}\bthermal\b|\bthermal\b[^.?!]{0,40}\befficien\w*\b")

EFF_SQL = """
SELECT station_key AS station, ROUND(wa_gross_heat_rate_kcal_kwh, 0) AS wa_gross_heat_rate_kcal_kwh,
       ROUND(sp_coal_kg_kwh, 3) AS sp_coal_kg_kwh, ROUND(aux_pct, 2) AS aux_pct, ROUND(plf_pct, 2) AS plf_pct
FROM mart_fact_station_generation_fy WHERE fy = ? AND plant_type_code = 'THERMAL'
ORDER BY wa_gross_heat_rate_kcal_kwh"""


async def thermal_efficiency(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _EFF_RE.search(q) or e.get("stations"):
        return None
    fy = _fy(e)
    if fy is None:
        return None
    res = await _q(db, EFF_SQL, [fy], t)
    r = _dicts(res)
    best, worst = r[0], r[-1]
    head = (f"{best['station']} was the most efficient thermal station in FY {fy}, with the lowest heat rate, "
            f"{_v('x_kcal_kwh', best['wa_gross_heat_rate_kcal_kwh'])}; {worst['station']} was the least efficient at "
            f"{_v('x_kcal_kwh', worst['wa_gross_heat_rate_kcal_kwh'])}.")
    body = _lines([f"{x['station']} — heat rate {_v('x_kcal_kwh', x['wa_gross_heat_rate_kcal_kwh'])}, specific coal "
                   f"{_v('x_kg_kwh', x['sp_coal_kg_kwh'])}, aux {_v('x_pct', x['aux_pct'])}, PLF {_v('x_pct', x['plf_pct'])}"
                   for x in r])
    note = "Efficiency is judged by gross heat rate (lower is better), with specific coal and auxiliary consumption alongside."
    chart = build_chart("bar", "station", "wa_gross_heat_rate_kcal_kwh", "Gross heat rate (kcal/kWh)", res.columns, res.rows)
    return Report("thermal_efficiency", f"{_b(head)}\n\n{body}\n\n{note}", EFF_SQL, res.columns, res.rows, chart)


# ---------------------------------------------------------------------------------------------- 12. thermal vs hydro over years
_TVH_RE = re.compile(r"\b(thermal\b[^.?!]{0,30}\bhydro|hydro\w*\b[^.?!]{0,30}\bthermal)\b[^.?!]{0,60}\b(years|trend|over time|"
                     r"each year|every year|by year|decade)\b")

TVH_SQL = """
SELECT fy, ROUND(SUM(CASE WHEN plant_type_code = 'THERMAL' THEN gross_gen_mu END), 3) AS thermal_mu,
       ROUND(SUM(CASE WHEN plant_type_code = 'HYDRO' THEN gross_gen_mu END), 3) AS hydro_mu,
       ROUND(SUM(CASE WHEN plant_type_code NOT IN ('THERMAL', 'HYDRO') THEN gross_gen_mu END), 3) AS other_mu
FROM mart_fact_station_generation_fy
GROUP BY fy ORDER BY fy"""


async def thermal_vs_hydro(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _TVH_RE.search(q) or e.get("stations"):
        return None
    res = await _q(db, TVH_SQL, [], t)
    r = [x for x in _dicts(res) if x["fy"] != "2025-26"]
    m = re.search(r"\b(last|past)\s+(\d+|five|three|ten)\s+years\b", q)
    n = {"five": 5, "three": 3, "ten": 10}.get(m.group(2), int(m.group(2)) if m and m.group(2).isdigit() else 0) if m else 0
    if n:
        r = r[-n:]
    last = r[-1]
    tot = (last["thermal_mu"] or 0) + (last["hydro_mu"] or 0) + (last["other_mu"] or 0)
    head = (f"In FY {last['fy']} thermal stations generated {_v('x_mu', last['thermal_mu'])} ({last['thermal_mu'] / tot * 100:.1f}%) "
            f"and hydro {_v('x_mu', last['hydro_mu'])} ({last['hydro_mu'] / tot * 100:.1f}%).")
    body = _lines([f"FY {x['fy']} — thermal {_v('x_mu', x['thermal_mu'])}, hydro {_v('x_mu', x['hydro_mu'])}, "
                   f"other {_v('x_mu', x['other_mu'])}" for x in r])
    rows = [tuple(x.values()) for x in r]
    chart = build_chart("bar", "fy", "thermal_mu", "Thermal generation (MU)", res.columns, rows)
    return Report("thermal_vs_hydro", f"{_b(head)}\n\n{body}", TVH_SQL, res.columns, rows, chart)


# ---------------------------------------------------------------------------------------------- 13. unit status now
_STATUS_RE = re.compile(r"\b(been (out|down|off|shut|under)|how long\b[^.?!]{0,40}\b(out|down|off|shut|outage|stopped)|since when|"
                        r"is\b[^.?!]{0,30}\b(running|working|available|in service|out of service|down|online|offline)|"
                        r"(current|present) status|status (now|today|of))\b")

STATUS_SQL = """
SELECT u.unit_key AS unit, d.capacity_mw, u.status, substr(u.status_since, 1, 10) AS status_since,
       ROUND(u.days_in_status, 0) AS days_in_status, u.reason_detail
FROM mart_fact_unit_current_status u JOIN mart_dim_unit d ON d.unit_key = u.unit_key
WHERE {cond}
ORDER BY u.unit_key"""


async def unit_status(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    if not _STATUS_RE.search(q) or not (e.get("units") or len(e.get("stations", [])) == 1):
        return None
    if any(p["kind"] != "date" or p["value"] != "2025-04-30" for p in e.get("periods", [])):
        return None  # a past period is a history question, not a status question
    if e.get("units"):
        marks = ",".join("?" * len(e["units"]))
        cond, params = f"u.unit_key IN ({marks})", list(e["units"])
    else:
        cond, params = "u.station_key = ?", [e["stations"][0]]
    sql = STATUS_SQL.format(cond=cond)
    res = await _q(db, sql, params, t)
    r = _dicts(res)
    if not r:
        return None
    pretty = lambda d: f"{d[8:10]}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(d[5:7]) - 1]}-{d[:4]}"  # noqa: E731
    out = [x for x in r if x["status"] != "RUNNING"]
    if len(r) == 1:
        x = r[0]
        head = (f"{x['unit']} has been {x['status'].replace('_', ' ').lower()} since {pretty(x['status_since'])}, "
                f"{int(x['days_in_status'])} days as on 30-Apr-2025." if x["status"] != "RUNNING" else
                f"{x['unit']} was running on 30-Apr-2025 (in service since {pretty(x['status_since'])}).")
        body = x["reason_detail"] or ""
    else:
        st = e["stations"][0] if e.get("stations") else r[0]["unit"].rsplit("-U", 1)[0]
        head = (f"{len(out)} of {st}'s {len(r)} units were out of service on 30-Apr-2025: "
                + ", ".join(f"{x['unit']} ({x['status'].replace('_', ' ').lower()} since {pretty(x['status_since'])}, "
                            f"{int(x['days_in_status'])} days)" for x in out) + "."
                if out else f"All {len(r)} {st} units were running on 30-Apr-2025.")
        body = ""
    return Report("unit_status", "\n\n".join(p for p in (_b(head), body, "'Now' means the last day in the data, 30-Apr-2025.") if p),
                  sql, res.columns, res.rows)


# ---------------------------------------------------------------------------------------------- 14. station and unit lists
_LIST_RE = re.compile(r"\b(?:list|show|name|what are|which are|give me|tell me|details (?:of|on|about)|"
                      r"information (?:on|about)|describe)\b[^.?!]{0,30}\b(?:all |the |every |kpcl'?s? )*"
                      r"(?P<noun>stations|plants|power stations|power plants|generating stations|units|fleet)\b")

STATIONS_SQL = """
SELECT station_key AS station, station_name, plant_category_label AS category, installed_capacity_mw AS capacity_mw,
       unit_count AS units, company_code AS company
FROM mart_dim_station WHERE {cond} ORDER BY category_order, report_order"""
UNITS_SQL = """
SELECT d.unit_key AS unit, d.capacity_mw, d.fuel_label AS fuel, d.technology, d.commissioning_date, s.status AS status_now
FROM mart_dim_unit d LEFT JOIN mart_fact_unit_current_status s ON s.unit_key = d.unit_key
WHERE d.station_key = ? ORDER BY d.unit_no"""


async def entity_list(q: str, e: dict, db: ReadOnlyDB, t: float) -> Report | None:
    m = _LIST_RE.search(q)
    if not m or e.get("metrics") or e.get("periods") or e.get("units"):
        return None
    if m.group("noun") == "units":
        if len(e.get("stations", [])) != 1:
            return None
        st = e["stations"][0]
        res = await _q(db, UNITS_SQL, [st], t)
        r = _dicts(res)
        head = f"{st} has {len(r)} unit{'s' if len(r) != 1 else ''} with {_v('x_mw', sum(x['capacity_mw'] for x in r))} in all."
        body = _lines([f"{x['unit']} — {_v('x_mw', x['capacity_mw'])}, commissioned {x['commissioning_date']}, "
                       f"{(x['status_now'] or '').replace('_', ' ').lower()} on 30-Apr-2025" for x in r])
        return Report("unit_list", f"{_b(head)}\n\n{body}", UNITS_SQL, res.columns, res.rows)
    if e.get("stations"):
        return None
    cond, params, words = _scope_filter(e)
    sql = STATIONS_SQL.format(cond=cond)
    res = await _q(db, sql, params, t)
    r = _dicts(res)
    who = "KPCL has" if words == "all" else f"KPCL has {len(r)} {words}"
    head = (f"{who} {len(r)} stations with {_v('x_mw', sum(x['capacity_mw'] for x in r))} installed and "
            f"{sum(x['units'] for x in r)} units." if words == "all" else
            f"{who} station{'s' if len(r) != 1 else ''} with {_v('x_mw', sum(x['capacity_mw'] for x in r))} installed.")
    body = _lines([f"{x['station']} — {x['station_name']}, {_CAT_LABEL.get(x['category'], x['category'])}, "
                   f"{_v('x_mw', x['capacity_mw'])}, {x['units']} unit{'s' if x['units'] != 1 else ''}" for x in r])
    note = ("YTPS belongs to RPCL and YCCPP to KPCGPCL, both KPCL subsidiaries; they are counted in every KPCL total."
            if words == "all" else "")
    return Report("station_list", "\n\n".join(p for p in (_b(head), body, note) if p), sql, res.columns, res.rows)


# ---------------------------------------------------------------------------------------------- dispatch
_REPORTS = [period_overview, unit_status, entity_list, units_at_risk, attention, coal_situation, reservoir_position, hydro_wy_compare,
            thermal_vs_hydro, share_trend, outage_trend, month_extreme, thermal_efficiency, station_scorecard, fy_summary]


# A request for figures that names KPCL or a station still wants the data ("KPCL summary", "RTPS overview"), except
# questions about the organisation itself, which the knowledge base answers (history, board, CSR, people ...).
FIGURES_ASK = re.compile(r"\b(reports?|mis|summary|summari[sz]e|overview|at a glance|scorecard|highlights|snapshot|performance|"
                         r"key figures|kpis?|dashboard|statistics|stats|figures|numbers)\b", re.I)
# topics about the organisation, never answered with the generation figures
PROFILE_TOPIC = re.compile(r"\b(history|historical|founded|established|incorporated|formed|headquarter\w*|mission|vision|"
                           r"chairman|chairperson|managing director|directors?|ceo|board|governance|employees|staff|workforce|"
                           r"recruit\w*|jobs?|careers?|contact|address|website|profile|background|origin|ownership|owned|"
                           r"subsidiar\w+|csr|corporate social|social responsibility|awards?|milestones?|tenders?|policy|"
                           r"policies|share ?holders?|upcoming projects?|new projects?|future projects?)\b", re.I)
# ... plus the question forms that ask about what KPCL or a station is, for choosing the knowledge base
PROFILE_ASK = re.compile(PROFILE_TOPIC.pattern[:-3] + r"|who|when was|what is|what are|what does|role of|functions?)\b", re.I)

# Measures a question can name, and the words that show an answer covers them (in its columns, row labels or text).
# A reviewed report that does not cover a measure the question names is skipped, so "coal stock trend" or
# "RTPS outages summary" never come back as a generation answer.
_MEASURES = [
    (re.compile(r"\bplf\b|plant load factor|load factor"), ("plf", "load factor")),
    (re.compile(r"\bpaf\b|availability"), ("paf", "availab")),
    (re.compile(r"\baux\w*"), ("aux",)),
    (re.compile(r"\bstocks?\b|days of stock"), ("stock",)),
    (re.compile(r"\boutages?\b|shutdowns?|breakdowns?"), ("outage", "shutdown", "out of service", "breakdown")),
    (re.compile(r"\btrips?\b|tripping"), ("trip",)),
    (re.compile(r"heat rate"), ("heat",)),
    (re.compile(r"\bgcv\b|calorific"), ("gcv", "calorific")),
    (re.compile(r"\btargets?\b"), ("target",)),
    (re.compile(r"reservoirs?|water levels?|\bdams?\b|storage"), ("reservoir", "level", "storage", "capacity_pct")),
    (re.compile(r"\bcoal\b"), ("coal",)),
    (re.compile(r"back[- ]?down|\blosses\b|\bloss\b"), ("loss", "back-down", "backdown", "back down")),
    (re.compile(r"\bheat\b|\bfuel\b|\boil\b|\bhfo\b|\bldo\b"), ("fuel", "oil", "hfo", "ldo", "heat")),
]
# reports that fall back to the latest complete FY when the question names no period
_DEFAULTS_TO_LATEST_FY = {"station_scorecard", "attention", "units_at_risk", "coal_situation", "month_extreme", "thermal_efficiency",
                          "fy_summary", "generation_breakdown:monthly", "generation_breakdown:weekly", "generation_breakdown:daily",
                          "generation_breakdown:quarterly", "generation_breakdown:station", "generation_breakdown:unit"}
DEFAULT_PERIOD_NOTE = f"No period was named, so this uses the latest complete financial year, FY {LATEST_COMPLETE_FY}."


def covers_asked_measures(q: str, rep: Report) -> bool:
    """Every measure the question names shows up in the report's columns, row labels or answer text."""
    seen = " ".join([rep.answer.lower(), " ".join(rep.columns).lower().replace("_", " "), " ".join(rep.columns).lower()]
                    + [str(v).lower() for row in rep.rows[:60] for v in row if isinstance(v, str)])
    return all(any(w in seen for w in words) for rx, words in _MEASURES if rx.search(q))


async def match_and_run(question: str, entities: dict, db: ReadOnlyDB, timeout_seconds: float) -> Report | None:
    if any(p.get("kind") == "out_of_range" for p in entities.get("periods", [])):
        return None
    from app.breakdown import generation_breakdown   # late import: breakdown.py builds this module's Report
    q = norm(question)
    # the breakdown runs after the specific reports (lists, scorecards, extremes) and before the FY summary
    for fn in _REPORTS[:-1] + [generation_breakdown] + _REPORTS[-1:]:
        rep = await fn(q, entities, db, timeout_seconds)
        if rep is None:
            continue
        if not covers_asked_measures(q, rep):
            continue   # a report that does not answer the measure asked about is never used
        if not entities.get("periods") and rep.key in _DEFAULTS_TO_LATEST_FY:
            rep.answer += "\n\n" + DEFAULT_PERIOD_NOTE
        return rep
    return None


def report_names() -> list[str]:
    return [fn.__name__ for fn in _REPORTS[:-1]] + ["generation_breakdown"] + [fn.__name__ for fn in _REPORTS[-1:]]

