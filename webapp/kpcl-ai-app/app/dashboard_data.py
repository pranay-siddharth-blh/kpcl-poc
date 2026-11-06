"""
Dashboard summary for GET /api/dashboard (pattern from the Cescom POC's dashboard_data.py).

Every query here is fixed and hand-reviewed, never model-written. They still run with restricted=True, so the same
SQLite authorizer as the chatbot applies (mart_* reads only), and every value that comes from a request (the FY and
the station) is first checked against the list the database itself returns, then passed as a bound parameter.

The formulas are the metric registry's, so the dashboard and the chatbot always agree:
  PLF % = SUM(gross_gen_mu) / SUM(capacity_mu) x 100          aux % = SUM(aux_consumption_mu) / SUM(gross_gen_mu) x 100
  CEA target achievement = SUM(gross_gen_mu) / SUM(cea_target_mu) x 100
  KPCL share of state consumption = SUM(kpcl_gross_gen_mu) / SUM(state_consumption_mu) x 100
Ratios are always recomputed from summed totals, never averaged from per-row percentages.

Two kinds of period appear on the dashboard and are labelled as such:
  * the selected financial year (default: the latest complete one, FY 2024-25) for generation, PLF, targets, losses;
  * "as on" the last day in the data (30-Apr-2025) for unit status, coal stock and reservoir levels.
"""
from __future__ import annotations

import datetime as dt

from app.db import ReadOnlyDB

_FYS_SQL = """
    SELECT fy, COUNT(DISTINCT year_month) AS months FROM mart_fact_station_generation_monthly GROUP BY fy ORDER BY fy
"""
_AS_OF_SQL = "SELECT MAX(date_key) AS as_of FROM mart_fact_station_generation_daily"
_STATIONS_SQL = """
    SELECT station_key, station_name, plant_category_label, plant_type_code, installed_capacity_mw, unit_count,
           fed_by_reservoirs, category_order, report_order
    FROM mart_dim_station ORDER BY category_order, report_order
"""

# {scope} is one of two fixed strings chosen in code ("" or "AND station_key = ?"), never request text.
_FY_TOTALS_SQL = """
    SELECT fy, SUM(gross_gen_mu) AS gen, SUM(capacity_mu) AS cap, SUM(aux_consumption_mu) AS aux,
           SUM(cea_target_mu) AS target, SUM(CASE WHEN cea_target_mu IS NOT NULL THEN gross_gen_mu END) AS gen_with_target,
           SUM(CASE WHEN plant_type_code = 'THERMAL' THEN gross_gen_mu END) AS thermal_gen,
           SUM(CASE WHEN plant_type_code = 'THERMAL' THEN capacity_mu END) AS thermal_cap,
           SUM(CASE WHEN plant_type_code = 'HYDRO' THEN gross_gen_mu END) AS hydro_gen,
           SUM(forced_outage_hrs) AS forced_hrs, SUM(trips) AS trips, SUM(coal_consumed_mt) AS coal_mt
    FROM mart_fact_station_generation_fy WHERE 1 = 1 {scope}
    GROUP BY fy ORDER BY fy
"""
# Daily totals for the selected FY and the one before, with the calendar week each day belongs to (weeks run Monday to
# Sunday, as in the workbook's calendar sheet). Weekly figures are the sums of these days.
_DAILY_SQL = """
    SELECT g.fy, g.date_key, d.week_start, d.day_name, SUM(g.gross_gen_mu) AS gen, SUM(g.capacity_mu) AS cap
    FROM mart_fact_station_generation_daily g JOIN mart_dim_date d ON d.date_key = g.date_key
    WHERE g.fy IN (?, ?) {scope}
    GROUP BY g.fy, g.date_key, d.week_start, d.day_name ORDER BY g.date_key
"""
_MONTHLY_SQL = """
    SELECT fy, year_month, month_label, SUM(gross_gen_mu) AS gen, SUM(cea_target_mu) AS target,
           SUM(capacity_mu) AS cap
    FROM mart_fact_station_generation_monthly WHERE fy IN (?, ?) {scope}
    GROUP BY fy, year_month, month_label ORDER BY year_month
"""
_MIX_SQL = """
    SELECT plant_category_label AS category, SUM(gross_gen_mu) AS gen
    FROM mart_fact_station_generation_fy WHERE fy = ? GROUP BY plant_category_label ORDER BY gen DESC
"""
_STATION_TABLE_SQL = """
    SELECT s.station_key, SUM(s.gross_gen_mu) AS gen, SUM(s.capacity_mu) AS cap, SUM(s.cea_target_mu) AS target,
           SUM(s.aux_consumption_mu) AS aux,
           (SELECT SUM(p.gross_gen_mu) FROM mart_fact_station_generation_fy p
             WHERE p.station_key = s.station_key AND p.fy = ?) AS prev_gen
    FROM mart_fact_station_generation_fy s WHERE s.fy = ?
    GROUP BY s.station_key
"""
_UNIT_TABLE_SQL = """
    SELECT m.unit_key, MAX(m.capacity_mw) AS capacity_mw, SUM(m.gross_gen_mu) AS gen, SUM(m.capacity_mu) AS cap,
           SUM(m.declared_capacity_mu) AS dc, SUM(m.running_hrs) AS running_hrs, SUM(m.trips) AS trips,
           SUM(m.forced_outage_hrs) AS forced_hrs
    FROM mart_fact_unit_generation_monthly m WHERE m.fy = ? AND m.station_key = ?
    GROUP BY m.unit_key ORDER BY LENGTH(m.unit_key), m.unit_key
"""
_UNIT_STATUS_SQL = """
    SELECT u.unit_key, u.station_key, u.plant_category_label, u.status, u.status_since, u.days_in_status,
           u.reason_detail, d.capacity_mw
    FROM mart_fact_unit_current_status u JOIN mart_dim_unit d ON d.unit_key = u.unit_key
    WHERE 1 = 1 {scope}
    ORDER BY u.days_in_status DESC
"""
_LOSSES_SQL = """
    SELECT l.reason_description AS reason, r.reason_class, SUM(l.loss_mu) AS loss
    FROM mart_fact_generation_loss_daily l JOIN mart_dim_outage_reason r ON r.reason_code = l.reason_code
    WHERE l.fy = ? {scope}
    GROUP BY l.reason_description, r.reason_class ORDER BY loss DESC LIMIT 8
"""
_COAL_SQL = """
    SELECT station_key, closing_stock_mt, days_of_stock, normative_days, is_below_norm
    FROM mart_fact_coal_stock_daily
    WHERE date_key = (SELECT MAX(date_key) FROM mart_fact_coal_stock_daily) {scope}
    ORDER BY days_of_stock
"""
_COAL_TREND_SQL = """
    SELECT year_month, month_label, AVG(days_of_stock) AS avg_days, MIN(normative_days) AS norm
    FROM mart_fact_coal_stock_daily WHERE fy = ? {scope}
    GROUP BY year_month, month_label ORDER BY year_month
"""
_RESERVOIRS_SQL = """
    SELECT reservoir_name, water_level, full_reservoir_level, min_draw_down_level, capacity_pct, energy_potential_mu,
           feeds_stations, level_unit
    FROM mart_fact_reservoir_daily
    WHERE date_key = (SELECT MAX(date_key) FROM mart_fact_reservoir_daily)
    ORDER BY capacity_pct DESC
"""
_OUT_STATIONS_SQL = "SELECT DISTINCT station_key FROM mart_fact_unit_current_status WHERE status <> 'RUNNING'"
_GRID_SQL = """
    SELECT fy, SUM(kpcl_gross_gen_mu) AS kpcl, SUM(state_consumption_mu) AS state, MAX(system_peak_mw) AS peak
    FROM mart_fact_grid_daily WHERE fy IN (?, ?) GROUP BY fy
"""


class DashboardNotFound(ValueError):
    """An FY or station in the request that the database doesn't have (-> 404)."""


def _dicts(qr) -> list[dict]:
    return [dict(zip(qr.columns, r)) for r in qr.rows]


def _ratio(num, den, scale: float = 100.0) -> float | None:
    if num is None or not den:
        return None
    return round(num / den * scale, 2)


def _pct_change(cur, prev) -> float | None:
    if cur is None or not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _r(v, nd: int = 3):
    return None if v is None else round(v, nd)


def _fmt(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


async def build_dashboard(db: ReadOnlyDB, fy: str | None, station: str | None, timeout_seconds: float) -> dict:
    async def q(sql: str, params: tuple = (), max_rows: int = 500):
        return _dicts(await db.run(sql, max_rows=max_rows, timeout_seconds=timeout_seconds, restricted=True,
                                   params=params))

    fy_rows = await q(_FYS_SQL)
    fys = [r["fy"] for r in fy_rows]
    complete = [r["fy"] for r in fy_rows if r["months"] == 12]
    if fy is None:
        fy = complete[-1] if complete else fys[-1]
    if fy not in fys:
        raise DashboardNotFound(f"No data for FY {fy!r}.")
    prev_fy = fys[fys.index(fy) - 1] if fys.index(fy) > 0 else None
    as_of = (await q(_AS_OF_SQL))[0]["as_of"]

    stations = await q(_STATIONS_SQL)
    by_key = {s["station_key"]: s for s in stations}
    if station is not None and station not in by_key:
        raise DashboardNotFound(f"No station {station!r}.")
    st = by_key.get(station)
    scope_sql = "AND station_key = ?" if st else ""
    scope_u = "AND u.station_key = ?" if st else ""
    scope_l = "AND l.station_key = ?" if st else ""
    sp = (station,) if st else ()

    # ---- KPIs and the FY-by-FY series behind the sparklines
    totals = await q(_FY_TOTALS_SQL.format(scope=scope_sql), sp)
    by_fy = {r["fy"]: r for r in totals}
    cur, prev = by_fy.get(fy, {}), by_fy.get(prev_fy, {}) if prev_fy else {}

    def kpis_for(r: dict) -> dict:
        return {"gen": r.get("gen"), "plf": _ratio(r.get("gen"), r.get("cap")), "aux": _ratio(r.get("aux"), r.get("gen")),
                "target": r.get("target"), "achievement": _ratio(r.get("gen_with_target"), r.get("target")),
                "thermal_plf": _ratio(r.get("thermal_gen"), r.get("thermal_cap")), "hydro_gen": r.get("hydro_gen"),
                "forced_hrs": r.get("forced_hrs"), "trips": r.get("trips"), "coal_mt": r.get("coal_mt")}

    k, kp = kpis_for(cur), kpis_for(prev)
    series_fys = [f for f in fys if f in complete or f == fy]
    series = {name: [{"fy": f, "v": kpis_for(by_fy.get(f, {}))[name]} for f in series_fys]
              for name in ("gen", "plf", "thermal_plf", "hydro_gen", "achievement", "aux")}

    grid = {}
    if not st:
        g = {r["fy"]: r for r in await q(_GRID_SQL, (fy, prev_fy or fy))}
        grid = {"share": _ratio(g.get(fy, {}).get("kpcl"), g.get(fy, {}).get("state")),
                "share_prev": _ratio(g.get(prev_fy, {}).get("kpcl"), g.get(prev_fy, {}).get("state")) if prev_fy else None,
                "peak_mw": g.get(fy, {}).get("peak")}

    # ---- unit status (as on the last day in the data)
    status_rows = await q(_UNIT_STATUS_SQL.format(scope=scope_u), sp)
    units_out = [{"unit": r["unit_key"], "station": r["station_key"], "category": r["plant_category_label"],
                  "status": r["status"], "since": (r["status_since"] or "")[:10], "days": _r(r["days_in_status"], 0),
                  "detail": r["reason_detail"], "capacity_mw": r["capacity_mw"]}
                 for r in status_rows if r["status"] != "RUNNING"]
    units_total = len(status_rows)
    mw_out = sum(u["capacity_mw"] or 0 for u in units_out)

    # ---- monthly trend: this FY vs the previous FY vs the CEA target
    monthly_rows = await q(_MONTHLY_SQL.format(scope=scope_sql), (fy, prev_fy or fy) + sp)
    cur_m = [r for r in monthly_rows if r["fy"] == fy]
    prev_m = [r for r in monthly_rows if r["fy"] == prev_fy]
    monthly = [{"label": _mon(r["month_label"], r["year_month"]),
                "month": r["month_label"], "gen": _r(r["gen"]), "target": _r(r["target"]),
                "plf": _ratio(r["gen"], r["cap"]),
                "prev": _r(prev_m[i]["gen"]) if i < len(prev_m) else None} for i, r in enumerate(cur_m)]

    # ---- daily and weekly: the same scope and FY, the previous FY aligned by position (day n / week n of the FY)
    day_rows = await q(_DAILY_SQL.format(scope=scope_sql.replace("station_key", "g.station_key")), (fy, prev_fy or fy) + sp,
                       max_rows=1000)
    cur_d = [r for r in day_rows if r["fy"] == fy]
    prev_d = [r for r in day_rows if r["fy"] == prev_fy] if prev_fy else []
    # a day is compared with the same date one year earlier (29 Feb has no partner)
    prev_by_date = {r["date_key"]: r for r in prev_d}
    daily = []
    for r in cur_d:
        y, md = r["date_key"][:4], r["date_key"][4:]
        p = prev_by_date.get(f"{int(y) - 1}{md}")
        daily.append({"date": r["date_key"], "day": r["day_name"][:3], "gen": _r(r["gen"]), "plf": _ratio(r["gen"], r["cap"]),
                      "prev": _r(p["gen"]) if p else None, "prev_date": p["date_key"] if p else None})

    def weeks(rows: list[dict]) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            if not out or out[-1]["week_start"] != r["week_start"]:
                out.append({"week_start": r["week_start"], "first": r["date_key"], "last": r["date_key"], "days": 0,
                            "gen": 0.0, "cap": 0.0})
            w = out[-1]
            w["last"], w["days"] = r["date_key"], w["days"] + 1
            w["gen"] += r["gen"] or 0
            w["cap"] += r["cap"] or 0
        return out
    cur_w = weeks(cur_d)
    # a week is compared with the same Monday-to-Sunday week 52 weeks (364 days) earlier, so weekdays line up
    prev_by_start = {w["week_start"]: w for w in weeks(prev_d)}
    weekly = []
    for i, w in enumerate(cur_w):
        p = prev_by_start.get((dt.date.fromisoformat(w["week_start"][:10]) - dt.timedelta(days=364)).isoformat())
        # the month a week belongs to: the month of its middle day (so a week counts where most of its days fall)
        mid = dt.date.fromisoformat(w["first"][:10]) + dt.timedelta(days=(w["days"] - 1) // 2)
        weekly.append({"week": i + 1, "month": mid.strftime("%b-%Y").upper(), "first": w["first"], "last": w["last"],
                       "days": w["days"], "gen": _r(w["gen"]),
                       "plf": _ratio(w["gen"], w["cap"]), "prev": _r(p["gen"]) if p else None,
                       "prev_days": p["days"] if p else None})

    # ---- losses by reason
    losses = [{"reason": r["reason"], "class": r["reason_class"], "loss": _r(r["loss"], 1)}
              for r in await q(_LOSSES_SQL.format(scope=scope_l), (fy,) + sp)]

    # ---- coal (thermal only) and reservoirs (hydro)
    thermal_scope = not st or st["plant_type_code"] == "THERMAL"
    coal = [{"station": r["station_key"], "stock_mt": _r(r["closing_stock_mt"], 0), "days": _r(r["days_of_stock"], 1),
             "norm": r["normative_days"], "below": bool(r["is_below_norm"])}
            for r in await q(_COAL_SQL.format(scope=scope_sql), sp)] if thermal_scope else []
    coal_trend = [{"label": _mon(r["month_label"], r["year_month"]), "days": _r(r["avg_days"], 1), "norm": r["norm"]}
                  for r in await q(_COAL_TREND_SQL.format(scope=scope_sql), (fy,) + sp)] if thermal_scope else []
    reservoirs = [{"name": r["reservoir_name"], "level": r["water_level"], "frl": r["full_reservoir_level"],
                   "mddl": r["min_draw_down_level"], "pct": _r(r["capacity_pct"], 1),
                   "energy_mu": _r(r["energy_potential_mu"], 0), "feeds": r["feeds_stations"], "unit": r["level_unit"]}
                  for r in await q(_RESERVOIRS_SQL)]
    if st:
        fed = {x.strip().upper() for x in (st["fed_by_reservoirs"] or "").split(",") if x.strip()}
        reservoirs = [r for r in reservoirs if r["name"].upper() in fed]

    out_stations = {r["station_key"] for r in await q(_OUT_STATIONS_SQL)}
    out: dict = {
        "as_of": as_of, "fy": fy, "prev_fy": prev_fy, "fys": fys, "complete_fys": complete,
        "is_partial": fy not in complete,
        "scope": ({"station": st["station_key"], "name": st["station_name"], "category": st["plant_category_label"],
                   "type": st["plant_type_code"], "capacity_mw": st["installed_capacity_mw"],
                   "unit_count": st["unit_count"]} if st else None),
        "kpis": {"cur": k, "prev": kp, "gen_yoy": _pct_change(k["gen"], kp["gen"]),
                 "installed_mw": st["installed_capacity_mw"] if st else sum(s["installed_capacity_mw"] or 0 for s in stations),
                 "station_count": 1 if st else len(stations), "units_total": units_total,
                 "units_out": len(units_out), "mw_out": mw_out, **grid},
        "series": series, "monthly": monthly, "weekly": weekly, "daily": daily, "losses": losses, "units_out": units_out,
        "coal": coal, "coal_trend": coal_trend, "reservoirs": reservoirs,
        "stations_index": [{"station": s["station_key"], "name": s["station_name"], "category": s["plant_category_label"],
                            "has_unit_out": s["station_key"] in out_stations} for s in stations],
    }

    if not st:
        out["mix"] = [{"category": r["category"], "gen": _r(r["gen"], 1)} for r in await q(_MIX_SQL, (fy,))]
        rows = {r["station_key"]: r for r in await q(_STATION_TABLE_SQL, (prev_fy or fy, fy))}
        out_by_station: dict[str, int] = {}
        for u in units_out:
            out_by_station[u["station"]] = out_by_station.get(u["station"], 0) + 1
        out["stations"] = [{
            "station": s["station_key"], "name": s["station_name"], "category": s["plant_category_label"],
            "type": s["plant_type_code"], "capacity_mw": s["installed_capacity_mw"], "units": s["unit_count"],
            "gen": _r(rows.get(s["station_key"], {}).get("gen"), 1),
            "plf": _ratio(rows.get(s["station_key"], {}).get("gen"), rows.get(s["station_key"], {}).get("cap")),
            "achievement": _ratio(rows.get(s["station_key"], {}).get("gen"), rows.get(s["station_key"], {}).get("target")),
            "yoy": _pct_change(rows.get(s["station_key"], {}).get("gen"), rows.get(s["station_key"], {}).get("prev_gen")),
            "units_out": out_by_station.get(s["station_key"], 0),
        } for s in stations]
    else:
        unit_rows = await q(_UNIT_TABLE_SQL, (fy, station))
        status = {r["unit_key"]: r for r in status_rows}
        out["units"] = [{"unit": r["unit_key"], "capacity_mw": r["capacity_mw"], "gen": _r(r["gen"], 1),
                         "plf": _ratio(r["gen"], r["cap"]), "paf": _ratio(r["dc"], r["cap"]),
                         "running_hrs": _r(r["running_hrs"], 0), "trips": r["trips"],
                         "status": status.get(r["unit_key"], {}).get("status")} for r in unit_rows]

    out["highlights"] = _highlights(out)
    return out


def _highlights(d: dict) -> list[dict]:
    """Plain-English takeaways, each with a tone (good / bad / info) for its marker. Deterministic, from the data."""
    k, cur, prev = d["kpis"], d["kpis"]["cur"], d["kpis"]["prev"]
    fy, pfy, st = d["fy"], d["prev_fy"], d["scope"]
    who = st["station"] if st else "KPCL"
    out: list[dict] = []
    partial = " (so far)" if d["is_partial"] else ""
    if cur.get("gen") is not None:
        yoy = k.get("gen_yoy")
        trend = (f", {'up' if yoy >= 0 else 'down'} {abs(yoy):.1f}% on FY {pfy}" if yoy is not None and not d["is_partial"] else "")
        out.append({"tone": "info" if yoy is None or d["is_partial"] else "good" if yoy >= 0 else "bad",
                    "text": f"{who} generated **{_fmt(cur['gen'], 1)} MU** in FY {fy}{partial}{trend}."})
    if st:
        if cur.get("plf") is not None:
            out.append({"tone": "info", "text": f"Plant load factor was **{_fmt(cur['plf'], 2)}%**"
                        + (f" (FY {pfy}: {_fmt(prev.get('plf'), 2)}%)." if prev.get("plf") is not None else ".")})
    elif cur.get("thermal_plf") is not None:
        diff = (cur["thermal_plf"] - prev["thermal_plf"]) if prev.get("thermal_plf") is not None else None
        out.append({"tone": "good" if (diff or 0) >= 0 else "bad",
                    "text": f"Thermal PLF was **{_fmt(cur['thermal_plf'], 2)}%**"
                    + (f", {abs(diff):.2f} points {'higher' if diff >= 0 else 'lower'} than FY {pfy}." if diff is not None else ".")})
    if cur.get("achievement") is not None:
        ach = cur["achievement"]
        text = f"CEA target achievement: **{_fmt(ach, 1)}%**"
        if not st and d.get("stations"):
            short = [s for s in d["stations"] if s["achievement"] is not None and s["achievement"] < 100]
            worst = min(short, key=lambda s: s["achievement"]) if short else None
            text += (f"; {len(short)} of {sum(1 for s in d['stations'] if s['achievement'] is not None)} stations fell short"
                     + (f", lowest {worst['station']} at {_fmt(worst['achievement'], 1)}%." if worst else "."))
        else:
            text += "."
        out.append({"tone": "good" if ach >= 100 else "bad", "text": text})
    if k["units_out"]:
        longest = max(d["units_out"], key=lambda u: u["days"] or 0)
        out.append({"tone": "bad", "text": f"**{k['units_out']} unit{'s' if k['units_out'] != 1 else ''} out of service** "
                    f"as on {_pretty(d['as_of'])} ({_fmt(k['mw_out'], 0)} MW); longest {longest['unit']}, "
                    f"{_fmt(longest['days'], 0)} days."})
    elif k["units_total"]:
        out.append({"tone": "good", "text": f"All {k['units_total']} units were available as on {_pretty(d['as_of'])}."})
    below = [c for c in d["coal"] if c["below"]]
    if d["coal"]:
        low = min(d["coal"], key=lambda c: c["days"] or 0)
        out.append({"tone": "bad" if below else "good",
                    "text": (f"Coal stock is **below the {low['norm']}-day norm at {len(below)} of {len(d['coal'])} "
                             f"thermal station{'s' if len(d['coal']) != 1 else ''}**; lowest {low['station']} at {_fmt(low['days'], 1)} days."
                             if below else f"Coal stock is within the norm at every thermal station in view.")})
    if d["reservoirs"] and (not st or st["type"] == "HYDRO"):
        lo = min(d["reservoirs"], key=lambda r: r["pct"] or 0)
        out.append({"tone": "info", "text": f"Reservoirs as on {_pretty(d['as_of'])}: lowest **{lo['name'].title()} at "
                    f"{_fmt(lo['pct'], 1)}%** of capacity."})
    if not st and k.get("share") is not None:
        out.append({"tone": "info", "text": f"KPCL met **{_fmt(k['share'], 1)}%** of Karnataka's electricity consumption in FY {fy}."})
    return out


def _mon(label: str | None, year_month: str) -> str:
    """'APR-2024' -> 'Apr' for chart axes."""
    return (label or year_month)[:3].title()


def _pretty(date_key: str | None) -> str:
    if not date_key:
        return "—"
    y, m, dd = date_key[:10].split("-")
    return f"{int(dd):02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m) - 1]}-{y}"


# ---------------------------------------------------------------------------------------------- insights page
def _card(group: str, topic: str, tone: str, value, unit: str, decimals: int, text: str, *, delta=None,
          delta_unit: str = "%", good_up: bool = True, series=None, icon: str = "") -> dict:
    return {"group": group, "topic": topic, "tone": tone, "value": value, "unit": unit, "decimals": decimals,
            "text": text, "delta": delta, "delta_unit": delta_unit, "good_up": good_up, "series": series, "icon": icon}


def _tone_delta(delta, good_up: bool = True) -> str:
    if delta is None or abs(delta) < 0.05:
        return "info"
    return "good" if (delta > 0) == good_up else "bad"


def _pct_change_1(cur, prev):
    return None if cur is None or not prev else round((cur - prev) / prev * 100, 1)


async def build_insights(db: ReadOnlyDB, fy: str | None, timeout_seconds: float) -> dict:
    """The key insights for one FY as KPI-style cards, built from the same reviewed queries as the dashboard."""
    d = await build_dashboard(db, fy, None, timeout_seconds)
    k, cur, prev = d["kpis"], d["kpis"]["cur"], d["kpis"]["prev"]
    fy, pfy, partial = d["fy"], d["prev_fy"], d["is_partial"]
    vs = f"FY {pfy}" if pfy else ""
    comparable = pfy is not None and not partial and bool(prev)
    cards: list[dict] = []

    def pts(a, b):
        return None if a is None or b is None or not comparable else round(a - b, 2)

    def since(v, fmt_nd, unit=""):
        return f"; {vs}: {_fmt(v, fmt_nd)}{unit}." if comparable and v is not None else "."

    # ---- performance
    yoy = k["gen_yoy"] if comparable else None
    cards.append(_card("Performance", "Gross generation", _tone_delta(yoy), cur["gen"], "MU", 1,
                       f"All 33 stations in FY {fy}{' so far' if partial else ''}" + since(prev.get("gen"), 1, " MU"),
                       delta=yoy, series=[x["v"] for x in d["series"]["gen"]], icon="bolt"))
    dp = pts(cur["thermal_plf"], prev.get("thermal_plf"))
    cards.append(_card("Performance", "Thermal PLF", _tone_delta(dp), cur["thermal_plf"], "%", 2,
                       "Plant load factor of the coal stations" + since(prev.get("thermal_plf"), 2, "%"),
                       delta=dp, delta_unit="pts", series=[x["v"] for x in d["series"]["thermal_plf"]], icon="gauge"))
    hy = _pct_change_1(cur["hydro_gen"], prev.get("hydro_gen")) if comparable else None
    cards.append(_card("Performance", "Hydro generation", _tone_delta(hy), cur["hydro_gen"], "MU", 1,
                       "Major and minor hydro stations" + since(prev.get("hydro_gen"), 1, " MU"),
                       delta=hy, series=[x["v"] for x in d["series"]["hydro_gen"]], icon="drop"))
    st = [s for s in d["stations"] if s["gen"] and s["plf"] is not None and (s["capacity_mw"] or 0) >= 20]
    if st:
        best = max(st, key=lambda s: s["plf"])
        cards.append(_card("Performance", "Best plant load factor", "good", best["plf"], "%", 2,
                           f"{best['station']} ({best['name']}) led the stations of 20 MW and above.", icon="trophy"))
    grow = [s for s in d["stations"] if s["yoy"] is not None and (s["capacity_mw"] or 0) >= 20]
    if grow and comparable:
        up, down = max(grow, key=lambda s: s["yoy"]), min(grow, key=lambda s: s["yoy"])
        cards.append(_card("Performance", "Biggest rise", "good" if up["yoy"] > 0 else "info", up["yoy"], "%", 1,
                           f"{up['station']} generated {_fmt(up['gen'], 1)} MU, the largest increase on {vs}.", icon="up"))
        cards.append(_card("Performance", "Biggest fall", "bad" if down["yoy"] < 0 else "info", down["yoy"], "%", 1,
                           f"{down['station']} generated {_fmt(down['gen'], 1)} MU, the largest drop on {vs}.", icon="down"))
    if len(d["monthly"]) > 1:
        bm = max(d["monthly"], key=lambda m: m["gen"] or 0)
        wm = min(d["monthly"], key=lambda m: m["gen"] or 0)
        cards.append(_card("Performance", "Best month", "info", bm["gen"], "MU", 1,
                           f"{(bm['month'] or '').title()} was the strongest month; the weakest was "
                           f"{(wm['month'] or '').title()} at {_fmt(wm['gen'], 1)} MU.", icon="calendar"))

    # ---- targets
    ach = cur["achievement"]
    if ach is not None:
        tgt = [s for s in d["stations"] if s["achievement"] is not None]
        short = [s for s in tgt if s["achievement"] < 100]
        worst = min(short, key=lambda s: s["achievement"]) if short else None
        cards.append(_card("Targets", "CEA target achievement", "good" if ach >= 100 else "bad", ach, "%", 1,
                           f"{_fmt(cur['gen'], 0)} of {_fmt(cur['target'], 0)} MU; {len(short)} of {len(tgt)} stations fell short"
                           + (f", lowest {worst['station']} at {_fmt(worst['achievement'], 1)}%." if worst else "."),
                           delta=pts(ach, prev.get("achievement")), delta_unit="pts",
                           series=[x["v"] for x in d["series"]["achievement"]], icon="target"))
        met = [s for s in tgt if s["achievement"] >= 100]
        cards.append(_card("Targets", "Stations on target", "good" if len(met) * 2 >= len(tgt) else "bad", len(met),
                           f"of {len(tgt)}", 0, "Stations that generated at least their CEA target"
                           + (": " + ", ".join(s["station"] for s in met[:6]) + "." if met else "; none did."), icon="check"))

    # ---- reliability
    out = d["units_out"]
    longest = max(out, key=lambda u: u["days"] or 0) if out else None
    cards.append(_card("Reliability", "Units out of service", "bad" if out else "good", k["units_out"], f"of {k['units_total']}", 0,
                       (f"{_fmt(k['mw_out'], 0)} MW unavailable on {_pretty(d['as_of'])}; longest {longest['unit']}, "
                        f"{_fmt(longest['days'], 0)} days." if out else f"All units available on {_pretty(d['as_of'])}."),
                       icon="power"))
    fh = _pct_change_1(cur["forced_hrs"], prev.get("forced_hrs")) if comparable else None
    cards.append(_card("Reliability", "Forced outage hours", _tone_delta(fh, good_up=False), cur["forced_hrs"], "hours", 0,
                       "Unit-hours lost to forced outages" + since(prev.get("forced_hrs"), 0, " hours"),
                       delta=fh, good_up=False, icon="alert"))
    tr = _pct_change_1(cur["trips"], prev.get("trips")) if comparable else None
    cards.append(_card("Reliability", "Unit trips", _tone_delta(tr, good_up=False), cur["trips"], "trips", 0,
                       "Trips across all units" + since(prev.get("trips"), 0), delta=tr, good_up=False, icon="zap"))
    if d["losses"]:
        top = d["losses"][0]
        total = sum(x["loss"] or 0 for x in d["losses"])
        kind = "back-down" if top["class"] == "BACKDOWN" else "outage"
        cards.append(_card("Reliability", "Largest generation loss", "bad", top["loss"], "MU", 1,
                           f"'{top['reason']}' ({kind}), {top['loss'] / total * 100:.0f}% of the {_fmt(total, 0)} MU lost "
                           f"to the top reasons.", icon="loss"))

    # ---- fuel and water (position on the last day in the data)
    if d["coal"]:
        low = min(d["coal"], key=lambda c: c["days"] or 0)
        below = [c for c in d["coal"] if c["below"]]
        cards.append(_card("Fuel and water", "Coal stock", "bad" if below else "good", low["days"], "days", 1,
                           f"Lowest: {low['station']} against a {low['norm']}-day norm; {len(below)} of {len(d['coal'])} "
                           f"thermal stations below norm on {_pretty(d['as_of'])}.", icon="coal"))
    if d["reservoirs"]:
        lo = min(d["reservoirs"], key=lambda r: r["pct"] or 0)
        hi = max(d["reservoirs"], key=lambda r: r["pct"] or 0)
        cards.append(_card("Fuel and water", "Lowest reservoir", "bad" if (lo["pct"] or 0) < 30 else "info", lo["pct"], "%", 1,
                           f"{lo['name'].title()} storage on {_pretty(d['as_of'])}; fullest {hi['name'].title()} at "
                           f"{_fmt(hi['pct'], 1)}%.", icon="drop"))

    # ---- efficiency and grid
    da = pts(cur["aux"], prev.get("aux"))
    cards.append(_card("Efficiency and grid", "Auxiliary consumption", _tone_delta(da, good_up=False), cur["aux"], "%", 2,
                       "Energy used by the stations themselves, as a share of generation (lower is better).",
                       delta=da, delta_unit="pts", good_up=False, series=[x["v"] for x in d["series"]["aux"]], icon="plug"))
    if k.get("share") is not None:
        ds = pts(k["share"], k.get("share_prev"))
        cards.append(_card("Efficiency and grid", "Share of Karnataka's consumption", _tone_delta(ds), k["share"], "%", 1,
                           f"KPCL generation against the state's consumption; state peak demand {_fmt(k['peak_mw'], 0)} MW.",
                           delta=ds, delta_unit="pts", icon="grid"))
    return {"fy": fy, "prev_fy": pfy, "as_of": d["as_of"], "fys": d["fys"], "complete_fys": d["complete_fys"],
            "is_partial": partial, "cards": cards, "stations_index": d["stations_index"],
            "summary": {t: sum(1 for c in cards if c["tone"] == t) for t in ("bad", "good", "info")}}
