"""
Generation breakdowns: "monthly / weekly / daily / quarterly / yearly / station-wise / unit-wise generation (trend)"
of KPCL or one station, for an FY (or one month, for daily and weekly).

Every figure comes from dashboard_data.build_dashboard() -- the same fixed, reviewed queries as the dashboard and its
MIS report -- so the chat answer, its table, its Excel report and the dashboard always agree. Weeks are Monday to
Sunday calendar weeks and a week belongs to the month its middle day falls in, exactly as on the dashboard.

Each breakdown also returns a total row computed from the year's own figures (ratios recomputed from summed totals,
never averaged), which the Excel report writes under the table together with an add-up check.
"""
from __future__ import annotations

import re

from app import dashboard_data as dd
from app.answer import build_chart, fmt_value
from app.db import ReadOnlyDB

LATEST_COMPLETE_FY = "2024-25"
_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_CAT_WORDS = {"THERMAL-COAL": "thermal (coal)", "MAJOR HYDRO": "major hydro", "MINOR HYDRO": "minor hydro", "GAS": "gas",
              "RES": "waste-to-energy", "SOLAR": "solar", "WIND": "wind"}

# the ways people ask for each breakdown
_GRAINS = [
    ("daily", r"daily|day[- ]?wise|day[- ]by[- ]day|each day|every day|per day|by day|date[- ]?wise|day[- ]?to[- ]?day"),
    ("weekly", r"weekly|week[- ]?wise|week[- ]by[- ]week|each week|every week|per week|by week|weeks"),
    ("quarterly", r"quarterly|quarter[- ]?wise|by quarters?|each quarter|every quarter|per quarter|quarters|q1\b.*q4"),
    ("monthly", r"monthly|month[- ]?wise|month[- ]by[- ]month|each month|every month|per month|by month|months"),
    ("yearly", r"yearly|year[- ]?wise|annual(ly)?|year[- ]by[- ]year|year[- ]on[- ]year|each year|every year|per year|by year|"
               r"fy[- ]?wise|over the (last )?(\w+ )?years|all (the )?years|financial years|across (the )?years|decade|history|historical"),
    ("unit", r"unit[- ]?wise|by units?|each unit|every unit|per unit|all (the )?units|units"),
    ("station", r"station[- ]?wise|plant[- ]?wise|by stations?|by plants?|each station|every station|per station|each plant|"
                r"every plant|all (the )?(stations|plants)|stations|plants"),
]
_GRAIN_RE = [(g, re.compile(rf"\b({p})\b")) for g, p in _GRAINS]
_TREND_RE = re.compile(r"\b(trend|trends|trending|pattern|profile|movement|variation|progress|over time|time series|series)\b")
_GEN_WORDS = re.compile(r"\b(generat\w*|gen|output|production|produced|energy|mu|million units|electricity)\b")
_REPORTISH = re.compile(r"\b(report|mis|data|figures|numbers|statistics|stats|details|breakdown|break[- ]?up|table|tabulate|"
                        r"list|sheet|excel|summary|overview|performance)\b")
# rankings and extremes have their own answers ("top 5 stations", "best month")
_NOT_A_BREAKDOWN = re.compile(r"\b(top|bottom|highest|lowest|best|worst|most|least|max(imum)?|min(imum)?|rank\w*|which|who|"
                              r"compare|comparison|versus|vs\.?|share|contribution|average)\b")
_OK_METRICS = {"gross_generation", "plf", "cea_target", "cea_target_achievement"}
# any other measure named means the question is not about generation / PLF, so it is left to the other routes
_OTHER_MEASURE = re.compile(r"\b(coal|stock|fuel|oil|hfo|ldo|gas consumption|reservoir|water|level|storage|inflow|rain\w*|"
                            r"outages?|trips?|tripping|aux\w*|heat rate|gcv|receipts?|consum\w*|loss(es)?|back[- ]?down|"
                            r"availability|paf|declared|peak|demand|frequency|btf|boiler|tube|emission|specific|net gen\w*|"
                            r"export|deemed|running hours?|shutdown|status|rakes?|supplier|grade|price|cost|revenue|tariff)\b")


def _grain(q: str, e: dict) -> str | None:
    for g, rx in _GRAIN_RE:
        if rx.search(q):
            if g == "unit" and len(e.get("stations", [])) != 1:
                continue
            return g
    if _TREND_RE.search(q):
        ps = e.get("periods", [])
        if not ps:
            return "yearly"
        if ps[0]["kind"] == "fy":
            return "monthly"
        if ps[0]["kind"] == "month":
            return "daily"
    return None


def _fy_of_month(value: str) -> tuple[str, str]:
    """'MAR-2025' -> ('2024-25', '2025-03')"""
    mon, yr = value.split("-")
    m, y = _MON.index(mon) + 1, int(yr)
    start = y if m >= 4 else y - 1
    return f"{start}-{(start + 1) % 100:02d}", f"{y}-{m:02d}"


def _v(col: str, v) -> str:
    return fmt_value(col, v)


def _r3(v):
    return None if v is None else round(v, 3)


def _pretty_day(d: str) -> str:
    return f"{int(d[8:10]):02d}-{_MON[int(d[5:7]) - 1].title()}-{d[:4]}"


def _month_title(label: str) -> str:
    return label[:1] + label[1:3].lower() + " " + label[4:]


async def generation_breakdown(q: str, e: dict, db: ReadOnlyDB, t: float):
    from app.reports import Report   # late import: reports.py dispatches to this module

    if e.get("units") or e.get("reservoirs") or len(e.get("stations", [])) > 1:
        return None
    metrics = set(e.get("metrics", []))
    if not metrics <= _OK_METRICS:
        return None
    grain = _grain(q, e)
    if grain is None or _NOT_A_BREAKDOWN.search(q) or _OTHER_MEASURE.search(q):
        return None
    if grain == "quarterly" and "plf" in metrics and "gross_generation" not in metrics:
        return None   # quarterly PLF needs capacity by quarter, which this breakdown does not carry
    if not metrics and not _GEN_WORDS.search(q):
        return None   # "turbine vibration trend", "list all stations": not a generation question
    cats, ptype = e.get("categories", []), e.get("plant_type")
    if (cats or ptype) and grain != "station":
        return None
    if len(cats) > 1:
        return None
    station = e["stations"][0] if e.get("stations") else None
    if grain == "station" and station:
        return None

    ps = e.get("periods", [])
    month = None
    if not ps:
        fy = LATEST_COMPLETE_FY
    elif len(ps) == 1 and ps[0]["kind"] == "fy":
        fy = ps[0]["value"]
    elif len(ps) == 1 and ps[0]["kind"] == "month" and grain in ("daily", "weekly"):
        fy, month = _fy_of_month(ps[0]["value"])
    else:
        return None
    if grain == "yearly" and ps and not (len(ps) == 1 and ps[0]["kind"] == "fy"):
        return None

    try:
        d = await dd.build_dashboard(db, fy, station, t)
    except dd.DashboardNotFound:
        return None
    who = station or "KPCL"
    k, kp = d["kpis"]["cur"], d["kpis"]["prev"]
    pfy, partial, as_of = d["prev_fy"], d["is_partial"], d["as_of"]
    so_far = f" (to {_pretty_day(as_of)}, the end of the data)" if partial else ""
    comparable = pfy is not None and not partial and kp.get("gen")

    async def q_sql(sql: str, params: tuple):
        return dd._dicts(await db.run(sql, max_rows=5000, timeout_seconds=t, restricted=True, params=params))

    lines: list[str] = []
    total = None
    # ------------------------------------------------------------------ monthly
    if grain == "monthly":
        cols = ["month", "gross_gen_mu", "cea_target_mu", "cea_target_achievement_pct", "plf_pct", "previous_fy_gen_mu", "yoy_growth_pct"]
        rows = [(m["month"], m["gen"], m["target"], dd._ratio(m["gen"], m["target"]) if m["target"] else None, m["plf"], m["prev"],
                 dd._pct_change(m["gen"], m["prev"])) for m in d["monthly"]]
        prev_sum = _r3(sum(m["prev"] for m in d["monthly"] if m["prev"] is not None)) if any(m["prev"] is not None for m in d["monthly"]) else None
        total = ("Total", _r3(k["gen"]), _r3(k["target"]), k["achievement"], k["plf"], prev_sum, dd._pct_change(k["gen"], prev_sum))
        noun, head = "months", f"{who} generated {_v('x_mu', k['gen'])} in FY {fy}{so_far}, month by month."
        if k["target"]:
            lines.append(f"CEA target {_v('x_mu', k['target'])}; achievement {_v('x_pct', k['achievement'])}. PLF for the year {_v('x_pct', k['plf'])}.")
        if comparable:
            lines.append(f"FY {pfy}: {_v('x_mu', kp['gen'])}, so {abs(d['kpis']['gen_yoy']):.1f}% {'more' if d['kpis']['gen_yoy'] >= 0 else 'less'} this year.")
        pts = [(_month_title(r[0]), r[1]) for r in rows if r[1] is not None]
        chart = build_chart("bar", "month", "gross_gen_mu", "Gross generation (MU)", cols, rows)
    # ------------------------------------------------------------------ weekly
    elif grain == "weekly":
        ws = [w for w in d["weekly"] if month is None or _month_key(w["month"]) == month]
        cols = ["week", "month", "from_date", "to_date", "day_count", "gross_gen_mu", "same_week_last_year_mu", "yoy_growth_pct", "plf_pct"]
        rows = [(f"W{w['week']}", w["month"], w["first"], w["last"], w["days"], w["gen"], w["prev"], dd._pct_change(w["gen"], w["prev"]), w["plf"])
                for w in ws]
        if not rows:
            return None
        gen_sum = _r3(sum(w["gen"] or 0 for w in ws))
        if month is None:
            total = ("Total", None, None, None, sum(w["days"] for w in ws), _r3(k["gen"]), None, None, k["plf"])
            head = f"{who} generated {_v('x_mu', k['gen'])} in FY {fy}{so_far}, over {len(ws)} calendar weeks (Monday to Sunday)."
        else:
            mlabel = _month_title(ws[0]["month"])
            cal = next((m["gen"] for m in d["monthly"] if _month_key(m["month"]) == month), None)
            total = ("Total", None, None, None, sum(w["days"] for w in ws), gen_sum, None, None, None)
            head = f"{who} generated {_v('x_mu', gen_sum)} in the {len(ws)} weeks counted in {mlabel}."
            lines.append(f"A week is counted in the month its middle day falls in, so these weeks run from {_pretty_day(ws[0]['first'])} to "
                         f"{_pretty_day(ws[-1]['last'])}; the calendar-month figure for {mlabel} is {_v('x_mu', cal)}.")
        part = [w for w in ws if w["days"] < 7]
        if part:
            lines.append("Part weeks: " + ", ".join(f"W{w['week']} ({w['days']} days)" for w in part) + "; they are left out of the highest / lowest below.")
        noun = "weeks"
        full = [w for w in ws if w["days"] == 7] or ws
        pts = [(f"W{w['week']} ({_pretty_day(w['first'])})", w["gen"]) for w in full if w["gen"] is not None]
        chart = build_chart("bar", "week", "gross_gen_mu", "Gross generation (MU)", cols, rows)
    # ------------------------------------------------------------------ daily
    elif grain == "daily":
        ds = [x for x in d["daily"] if month is None or x["date"][:7] == month]
        cols = ["date", "day", "gross_gen_mu", "plf_pct", "same_date_last_year_mu", "yoy_growth_pct"]
        rows = [(x["date"][:10], x["day"], x["gen"], x["plf"], x["prev"], dd._pct_change(x["gen"], x["prev"])) for x in ds]
        if not rows:
            return None
        if month is None:
            total = ("Total", None, _r3(k["gen"]), k["plf"], None, None)
            head = f"{who} generated {_v('x_mu', k['gen'])} in FY {fy}{so_far}, over {len(ds)} days."
        else:
            mm = next((m for m in d["monthly"] if _month_key(m["month"]) == month), None)
            total = ("Total", None, _r3(mm["gen"]) if mm else _r3(sum(x["gen"] or 0 for x in ds)), mm["plf"] if mm else None, None, None)
            head = f"{who} generated {_v('x_mu', total[2])} in {_month_title(mm['month']) if mm else month}, day by day ({len(ds)} days)."
        noun = "days"
        pts = [(_pretty_day(x["date"]), x["gen"]) for x in ds if x["gen"] is not None]
        chart = build_chart("line", "date", "gross_gen_mu", "Gross generation (MU)", cols, rows)
    # ------------------------------------------------------------------ quarterly
    elif grain == "quarterly":
        cols = ["quarter", "gross_gen_mu", "cea_target_mu", "cea_target_achievement_pct", "previous_fy_gen_mu", "yoy_growth_pct", "months_in_data"]
        qnames = [("Q1", "Apr–Jun"), ("Q2", "Jul–Sep"), ("Q3", "Oct–Dec"), ("Q4", "Jan–Mar")]
        rows = []
        for i, (qn, span) in enumerate(qnames):
            ms = d["monthly"][i * 3:(i + 1) * 3]
            if not ms:
                break
            gen = _r3(sum(m["gen"] or 0 for m in ms))
            tgt_ms = [m for m in ms if m["target"]]
            tgt = _r3(sum(m["target"] for m in tgt_ms)) if tgt_ms else None
            gen_t = sum(m["gen"] or 0 for m in tgt_ms)
            prev = _r3(sum(m["prev"] for m in ms if m["prev"] is not None)) if any(m["prev"] is not None for m in ms) else None
            rows.append((f"{qn} ({span})", gen, tgt, dd._ratio(gen_t, tgt) if tgt else None, prev, dd._pct_change(gen, prev), len(ms)))
        prev_sum = _r3(sum(r[4] for r in rows if r[4] is not None)) if any(r[4] is not None for r in rows) else None
        total = ("Total", _r3(k["gen"]), _r3(k["target"]), k["achievement"], prev_sum, dd._pct_change(k["gen"], prev_sum), sum(r[6] for r in rows))
        head = f"{who} generated {_v('x_mu', k['gen'])} in FY {fy}{so_far}, quarter by quarter."
        if partial:
            lines.append(f"FY {fy} is incomplete in the data, so only the months to {_pretty_day(as_of)} are included.")
        noun = "quarters"
        pts = [(r[0], r[1]) for r in rows if r[1] is not None]
        chart = build_chart("bar", "quarter", "gross_gen_mu", "Gross generation (MU)", cols, rows)
    # ------------------------------------------------------------------ yearly
    elif grain == "yearly":
        s = d["series"]
        by = {n: {x["fy"]: x["v"] for x in s[n]} for n in ("gen", "plf", "achievement")}
        fys = [x["fy"] for x in s["gen"]]
        if ps:   # "yearly generation up to FY 2023-24": the years up to the one named
            fys = [f for f in fys if f <= fy]
        cols = ["fy", "gross_gen_mu", "plf_pct", "cea_target_achievement_pct", "yoy_growth_pct"]
        rows, prev = [], None
        for f in fys:
            g = by["gen"].get(f)
            rows.append((f, _r3(g), by["plf"].get(f), by["achievement"].get(f), dd._pct_change(g, prev)))
            prev = g
        head = f"{who}'s gross generation by financial year, FY {fys[0]} to FY {fys[-1]}."
        if d["fys"][-1] not in fys:
            lines.append(f"FY {d['fys'][-1]} is left out because the data ends on {_pretty_day(as_of)}.")
        noun = "financial years"
        pts = [(f"FY {r[0]}", r[1]) for r in rows if r[1] is not None]
        chart = build_chart("bar", "fy", "gross_gen_mu", "Gross generation (MU)", cols, rows)
    # ------------------------------------------------------------------ station-wise
    elif grain == "station":
        ex = {r["station_key"]: r for r in await q_sql(dd._STATION_TABLE_SQL, (pfy or fy, fy))}
        sts = d["stations"]
        if cats:
            sts = [s for s in sts if s["category"] == cats[0]]
        elif ptype:
            sts = [s for s in sts if s["type"] == ptype]
        if not sts:
            return None
        cols = ["station", "station_name", "category", "capacity_mw", "gross_gen_mu", "plf_pct", "cea_target_mu", "cea_target_achievement_pct",
                "previous_fy_gen_mu", "yoy_growth_pct"]
        rows = []
        sums = {"cap_mw": 0.0, "gen": 0.0, "cap": 0.0, "target": 0.0, "gen_t": 0.0, "prev": 0.0}
        for s in sts:
            x = ex.get(s["station"], {})
            gen, prev = x.get("gen"), (x.get("prev_gen") if pfy else None)
            rows.append((s["station"], s["name"], _CAT_WORDS.get(s["category"], s["category"]).capitalize(), s["capacity_mw"], _r3(gen),
                         dd._ratio(gen, x.get("cap")), _r3(x.get("target")), dd._ratio(gen, x.get("target")) if x.get("target") else None,
                         _r3(prev), dd._pct_change(gen, prev)))
            sums["cap_mw"] += s["capacity_mw"] or 0
            sums["gen"] += gen or 0
            sums["cap"] += x.get("cap") or 0
            sums["prev"] += prev or 0
            if x.get("target"):
                sums["target"] += x["target"]
                sums["gen_t"] += gen or 0
        rows.sort(key=lambda r: -(r[4] or 0))
        total = ("Total", f"{len(rows)} stations", None, round(sums["cap_mw"], 3), _r3(sums["gen"]), dd._ratio(sums["gen"], sums["cap"]),
                 _r3(sums["target"]) or None, dd._ratio(sums["gen_t"], sums["target"]), _r3(sums["prev"]) if pfy else None,
                 dd._pct_change(sums["gen"], sums["prev"]) if pfy and not partial else None)
        scope = f"KPCL's {_CAT_WORDS.get(cats[0], cats[0].lower())} stations" if cats else f"KPCL's {ptype.lower()} stations" if ptype else "KPCL's stations"
        head = f"{scope[:1].upper() + scope[1:]} generated {_v('x_mu', sums['gen'])} in FY {fy}{so_far}, station by station."
        top = [r for r in rows if r[4]][:3]
        if top and sums["gen"]:
            lines.append("Largest: " + ", ".join(f"{r[0]} {_v('x_mu', r[4])} ({r[4] / sums['gen'] * 100:.1f}%)" for r in top) + ".")
        if sums["target"]:
            short = [r for r in rows if r[7] is not None and r[7] < 100]
            lines.append(f"CEA target achievement {_v('x_pct', total[7])}; {len(short)} of {sum(1 for r in rows if r[7] is not None)} stations fell short.")
        noun = "stations"
        pts = [(r[0], r[4]) for r in rows if r[4] is not None]
        chart = build_chart("bar", "station", "gross_gen_mu", "Gross generation (MU)", cols, rows[:20])
    # ------------------------------------------------------------------ unit-wise
    else:
        full = {r["unit_key"]: r for r in await q_sql(dd._UNIT_TABLE_SQL, (fy, station))}
        cols = ["unit", "capacity_mw", "gross_gen_mu", "plf_pct", "paf_pct", "running_hrs", "trips", "status_now"]
        rows = [(u["unit"], u["capacity_mw"], _r3(full.get(u["unit"], {}).get("gen")), u["plf"], u["paf"], u["running_hrs"], u["trips"],
                 (u["status"] or "").replace("_", " ").capitalize() or None) for u in d["units"]]
        if not rows:
            return None
        total = ("Total", round(sum(r[1] or 0 for r in rows), 3), _r3(k["gen"]), k["plf"], None, sum(r[5] or 0 for r in rows),
                 sum(r[6] or 0 for r in rows), None)
        head = f"{station}'s {len(rows)} units generated {_v('x_mu', k['gen'])} in FY {fy}{so_far}; station PLF {_v('x_pct', k['plf'])}."
        out = [r for r in rows if r[7] and r[7] != "Running"]
        if out:
            lines.append(f"Out of service on {_pretty_day(as_of)}: " + ", ".join(f"{r[0]} ({r[7].lower()})" for r in out) + ".")
        noun = "units"
        pts = [(r[0], r[2]) for r in rows if r[2] is not None]
        chart = build_chart("bar", "unit", "gross_gen_mu", "Gross generation (MU)", cols, rows)

    plf_led = "plf" in metrics and "gross_generation" not in metrics and "plf_pct" in cols
    if plf_led:
        i = cols.index("plf_pct")
        lab = {"month": lambda r: _month_title(r[0]), "week": lambda r: f"{r[0]} ({_pretty_day(r[2])})", "date": lambda r: _pretty_day(r[0]),
               "fy": lambda r: f"FY {r[0]}"}.get(cols[0], lambda r: r[0])
        pl = [(lab(r), r[i]) for r in rows if r[i] is not None and (cols[0] != "week" or r[4] == 7)]
        whole = total[i] if total and total[i] is not None else None
        by = {"monthly": "month by month", "weekly": "week by week", "daily": "day by day", "station": "station by station", "unit": "unit by unit"}
        head = (f"{who} PLF in FY {fy}{so_far}: {_v('x_pct', whole)}; {by.get(grain, grain)} below." if whole is not None and grain != "yearly"
                else f"{who} PLF by financial year, FY {rows[0][0]} to FY {rows[-1][0]}." if grain == "yearly" else head)
        if len(pl) > 1:
            hi, lo = max(pl, key=lambda p: p[1]), min(pl, key=lambda p: p[1])
            lines.insert(0, f"Highest PLF: {hi[0]}, {_v('x_pct', hi[1])}; lowest: {lo[0]}, {_v('x_pct', lo[1])}. "
                            "PLF for a period is recomputed from total generation and capacity, not averaged.")
        chart = build_chart("line" if grain in ("daily", "yearly", "monthly", "weekly") else "bar", cols[0], "plf_pct", "PLF (%)", cols, rows)
    elif pts and len(pts) > 1 and grain not in ("station",):
        hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
        avg = sum(p[1] for p in pts) / len(pts)
        each = {"months": "month", "weeks": "full week", "days": "day", "quarters": "quarter", "financial years": "year", "units": "unit"}[noun]
        lines.insert(0, f"Highest: {hi[0]}, {_v('x_mu', hi[1])}; lowest: {lo[0]}, {_v('x_mu', lo[1])}; average {_v('x_mu', avg)} per {each}.")
    lines.append(f"All {len(rows)} {noun} are in the table below.")
    answer = "\n\n".join([f"**{head}**"] + lines)
    return Report(f"generation_breakdown:{grain}", answer, "-- dashboard queries (app/dashboard_data.py)", cols, rows, chart, total)


def _month_key(label: str) -> str:
    """'MAR-2025' -> '2025-03'"""
    return f"{label[4:8]}-{_MON.index(label[:3]) + 1:02d}"
