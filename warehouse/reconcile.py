"""
Reconcile core_unit_daily_generation against document_anchors.json (SQLite port of
../reconcile_to_document.py steps 1-3 and ../verify_reconciliation.py, same rules):

  1. RTPS and SGS unit figures on 06-Apr-2025 are overwritten with the document's daily reports.
  2. RTPS March-2025 unit totals, then RTPS FY 2024-25 unit totals (March held fixed), are hit by a
     multiplicative rescale of the daily rows, so each day keeps its shape. Aux, coal, HFO and
     peak-hour generation scale with gross; declared capacity is lifted if needed so PAF >= PLF.
  3. SGS March-2025 unit totals, same rescale.
The HO-report FY/WY rescale (step 4 of the original) stays disabled for the reason recorded there:
the HO report's FY columns are April-to-date, not full-year totals.
"""
from __future__ import annotations

import sqlite3

_COLS = "gen_date, gross_gen_mu, aux_consumption_mu, coal_consumed_mt, hfo_consumed_kl, peak_hour_gen_mu"
_UPDATE = """UPDATE core_unit_daily_generation SET gross_gen_mu = ?, aux_consumption_mu = ?, coal_consumed_mt = ?,
             hfo_consumed_kl = ?, peak_hour_gen_mu = ?, declared_capacity_mu = MAX(COALESCE(declared_capacity_mu, 0), ?)
             WHERE unit_id = ? AND gen_date = ?"""


def _scaled(v, factor):
    return float(v) * factor if v else v


def _rescale(cur: sqlite3.Cursor, unit_id: str, rows: list, target: float, label: str, note) -> None:
    current = sum(float(r[1]) for r in rows)
    if not rows:
        note(f"  [{label}] {unit_id}: no rows in range -- skipped")
        return
    if current <= 1e-9:
        if target <= 1e-9:
            note(f"  [{label}] {unit_id}: both current and target ~0 -- consistent, no change")
        else:
            note(f"  [{label}] {unit_id}: CONTRADICTION -- current sum 0 but document target {target:.3f}; skipped")
        return
    factor = target / current
    for gd, g, a, c, h, p in rows:
        new_gross = float(g) * factor
        cur.execute(_UPDATE, (new_gross, _scaled(a, factor), _scaled(c, factor), _scaled(h, factor), _scaled(p, factor),
                              new_gross, unit_id, gd))
    note(f"  [{label}] {unit_id}: {len(rows)} days rescaled by x{factor:.4f} (was {current:.3f} MU, target {target:.3f} MU)")


def apply(con: sqlite3.Connection, anchors: dict, log) -> list[str]:
    lines: list[str] = []

    def note(msg: str) -> None:
        lines.append(msg)
        log("reconcile:" + msg)

    cur = con.cursor()
    note(" STEP 1: RTPS & SGS daily reports, 2025-04-06")
    for u, v in anchors["rtps_daily_report_2025_04_06"]["units"].items():
        unit_id = f"RTPS-{u}"
        if v.get("gen_mu") is None:
            g = cur.execute("SELECT gross_gen_mu FROM core_unit_daily_generation WHERE unit_id=? AND gen_date='2025-04-06'",
                            (unit_id,)).fetchone()
            if g and g[0] != 0:
                note(f"  {unit_id}: document shows outage, data has {g[0]} MU -> zeroing")
                cur.execute("""UPDATE core_unit_daily_generation SET gross_gen_mu=0, aux_consumption_mu=0, coal_consumed_mt=0,
                               hfo_consumed_kl=0, running_hrs=0, forced_outage_hrs=24, planned_outage_hrs=0, rsd_hrs=0,
                               peak_hour_gen_mu=0, peak_load_mw=0 WHERE unit_id=? AND gen_date='2025-04-06'""", (unit_id,))
            continue
        run = v.get("running_hrs", 24)
        cur.execute("""UPDATE core_unit_daily_generation SET gross_gen_mu=?, aux_consumption_mu=?, peak_hour_gen_mu=?,
                       peak_load_mw=?, running_hrs=?, forced_outage_hrs=?, planned_outage_hrs=0, rsd_hrs=0,
                       coal_consumed_mt=?, h2_cylinders_nos=?,
                       declared_capacity_mu = MAX(COALESCE(declared_capacity_mu, 0), ?)
                       WHERE unit_id=? AND gen_date='2025-04-06'""",
                    (v["gen_mu"], v["aux_mu"], v.get("peak_hr_gen_mu"), v.get("day_peak_load_mw"), run, 24 - run,
                     v.get("coal_consumed_mt"), v.get("h2_cylinders"), v["gen_mu"], unit_id))
    note("  RTPS units updated for 2025-04-06.")
    for u, gen in anchors["sgs_daily_report_2025_04_06"]["units_mu"].items():
        cur.execute("""UPDATE core_unit_daily_generation SET gross_gen_mu=?,
                       aux_consumption_mu = MIN(aux_consumption_mu, ?),
                       declared_capacity_mu = MAX(COALESCE(declared_capacity_mu, 0), ?)
                       WHERE unit_id=? AND gen_date='2025-04-06'""", (gen, gen, gen, f"SGS-{u}"))
    note("  SGS units updated for 2025-04-06.")
    con.commit()

    note(" STEP 2: RTPS MPR March 2025 (unit-month)")
    for u, v in anchors["rtps_mpr_march_2025"]["units"].items():
        if v.get("gross_gen_mu") is None:
            continue
        rows = cur.execute(f"SELECT {_COLS} FROM core_unit_daily_generation WHERE unit_id=? "
                           "AND gen_date BETWEEN '2025-03-01' AND '2025-03-31' ORDER BY gen_date", (f"RTPS-{u}",)).fetchall()
        _rescale(cur, f"RTPS-{u}", rows, v["gross_gen_mu"], "RTPS-MAR25", note)

    note(" STEP 2b: RTPS MPR FY 2024-25 (unit-FY, March held fixed)")
    for u, v in anchors["rtps_mpr_fy_2024_25"]["units"].items():
        if v.get("gross_gen_mu") is None:
            continue
        unit_id = f"RTPS-{u}"
        march = cur.execute("""SELECT COALESCE(SUM(gross_gen_mu), 0) FROM core_unit_daily_generation d
                               JOIN core_dim_date c ON c.cal_date = d.gen_date
                               WHERE unit_id=? AND c.fy='2024-25' AND c.month_label='MAR-2025'""", (unit_id,)).fetchone()[0]
        rows = cur.execute(f"""SELECT {_COLS.replace('gen_date', 'd.gen_date')} FROM core_unit_daily_generation d
                               JOIN core_dim_date c ON c.cal_date = d.gen_date
                               WHERE unit_id=? AND c.fy='2024-25' AND c.month_label<>'MAR-2025' ORDER BY d.gen_date""",
                           (unit_id,)).fetchall()
        _rescale(cur, unit_id, rows, v["gross_gen_mu"] - float(march), "RTPS-FY24-25", note)
    con.commit()

    note(" STEP 3: SGS MPR March 2025 (unit-month)")
    for u in anchors["sgs_mpr_march_2025"]["units"]:
        unit_id = f"SGS-U{u['unit_no']}"
        rows = cur.execute(f"SELECT {_COLS} FROM core_unit_daily_generation WHERE unit_id=? "
                           "AND gen_date BETWEEN '2025-03-01' AND '2025-03-31' ORDER BY gen_date", (unit_id,)).fetchall()
        _rescale(cur, unit_id, rows, u["gen_month_mu"], "SGS-MAR25", note)
    con.commit()
    note(" STEP 4 (HO-report FY/WY rescale) not applied: HO 'FY gen' columns are April-to-date, not full-year totals.")
    return lines


# Workbook 'anchors' sheet FY 2024-25 figures for the stations not anchored unit-by-unit (same 22 as
# ../verify_reconciliation.py).
WORKBOOK_FY2425 = {
    "BTPS": 8181.451, "YTPS": 6015.925, "YCCPP": 707.024, "BWTEP": 6.74, "NPH": 4096.355, "VUGPH": 1207.555,
    "MGHE": 443.609, "LPH": 178.93, "GERUSOPPA": 583.843, "SUPA": 646.78, "KODASALLI": 526.875, "KADRA": 532.268,
    "ALAMATTI": 630.111, "GHATPRABHA": 114.051, "BHADRA": 70.34, "MUNIRABAD": 122.455, "SHIVA": 107.835,
    "SHIMSHA": 31.104, "MALLAPUR": 44.542, "MALAPRABHA": 5.216, "KG-WEF": 5.458, "MANI": 28.169,
}

_STATION_DAY = ("SELECT SUM(gross_gen_mu) FROM core_unit_daily_generation d JOIN core_unit u USING (unit_id) "
                "WHERE u.station_code=? AND gen_date='2025-04-06'")
_UNIT_MONTH = ("SELECT SUM(gross_gen_mu) FROM core_unit_daily_generation d JOIN core_dim_date c ON c.cal_date=d.gen_date "
               "WHERE unit_id=? AND c.month_label=?")
_STATION_MONTH = ("SELECT SUM(gross_gen_mu) FROM core_unit_daily_generation d JOIN core_unit u USING (unit_id) "
                  "JOIN core_dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=? AND c.month_label=?")
_UNIT_FY = ("SELECT SUM(gross_gen_mu) FROM core_unit_daily_generation d JOIN core_dim_date c ON c.cal_date=d.gen_date "
            "WHERE unit_id=? AND c.fy=?")
_STATION_FY = ("SELECT SUM(gross_gen_mu) FROM core_unit_daily_generation d JOIN core_unit u USING (unit_id) "
               "JOIN core_dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=? AND c.fy=?")


def verify(con: sqlite3.Connection, a: dict) -> list[dict]:
    cur = con.cursor()
    results: list[dict] = []

    def scalar(sql, params=()):
        r = cur.execute(sql, params).fetchone()
        return r[0] if r else None

    def check(label, expected, actual, tol=0.01):
        if expected is None or actual is None:
            status, diff = "SKIP", None
        else:
            diff = float(actual) - float(expected)
            status = "PASS" if abs(diff) <= tol else "FAIL"
        results.append({"label": label, "expected": expected, "actual": None if actual is None else float(actual),
                        "diff": diff, "tol": tol, "status": status})

    for u, v in a["rtps_daily_report_2025_04_06"]["units"].items():
        if v.get("gen_mu") is not None:
            check(f"RTPS-{u} gross_gen 06-Apr-2025", v["gen_mu"],
                  scalar("SELECT gross_gen_mu FROM core_unit_daily_generation WHERE unit_id=? AND gen_date='2025-04-06'",
                         (f"RTPS-{u}",)))
    check("RTPS station gross_gen 06-Apr-2025", a["ho_daily_generation_report"]["stations"]["RTPS"][0], scalar(_STATION_DAY, ("RTPS",)))
    for u, gen in a["sgs_daily_report_2025_04_06"]["units_mu"].items():
        check(f"SGS-{u} gross_gen 06-Apr-2025", gen,
              scalar("SELECT gross_gen_mu FROM core_unit_daily_generation WHERE unit_id=? AND gen_date='2025-04-06'", (f"SGS-{u}",)))
    check("SGS station gross_gen 06-Apr-2025", a["sgs_daily_report_2025_04_06"]["station_gen_mu"], scalar(_STATION_DAY, ("SGS",)))

    for u, v in a["rtps_mpr_march_2025"]["units"].items():
        if v.get("gross_gen_mu") is not None:
            check(f"RTPS-{u} gross_gen MAR-2025", v["gross_gen_mu"], scalar(_UNIT_MONTH, (f"RTPS-{u}", "MAR-2025")))
    check("RTPS station gross_gen MAR-2025", a["rtps_mpr_march_2025"]["station"]["gross_gen_mu"],
          scalar(_STATION_MONTH, ("RTPS", "MAR-2025")))
    for u, v in a["rtps_mpr_fy_2024_25"]["units"].items():
        if v.get("gross_gen_mu") is not None:
            check(f"RTPS-{u} gross_gen FY2024-25", v["gross_gen_mu"], scalar(_UNIT_FY, (f"RTPS-{u}", "2024-25")))
    check("RTPS station gross_gen FY2024-25", a["rtps_mpr_fy_2024_25"]["station"]["gross_gen_mu"],
          scalar(_STATION_FY, ("RTPS", "2024-25")))
    check("RTPS-U1 gross_gen FY2024-25 (should be 0, forced out all year)", 0, scalar(_UNIT_FY, ("RTPS-U1", "2024-25")))

    for u in a["sgs_mpr_march_2025"]["units"]:
        uid = f"SGS-U{u['unit_no']}"
        check(f"{uid} gross_gen MAR-2025", u["gen_month_mu"], scalar(_UNIT_MONTH, (uid, "MAR-2025")))
    check("SGS station gross_gen MAR-2025", a["sgs_mpr_march_2025"]["station_totals"]["gen_month_mu"],
          scalar(_STATION_MONTH, ("SGS", "MAR-2025")))

    for station, expected in WORKBOOK_FY2425.items():
        check(f"{station} gross_gen FY2024-25 (workbook anchor)", expected, scalar(_STATION_FY, (station, "2024-25")), tol=0.5)

    check("Rows where hours don't sum to 24 (should be 0)", 0, scalar(
        "SELECT COUNT(*) FROM core_unit_daily_generation WHERE ABS(running_hrs+forced_outage_hrs+planned_outage_hrs+rsd_hrs-24) > 0.06"), 0)
    check("Rows where aux > gross (should be 0)", 0, scalar(
        "SELECT COUNT(*) FROM core_unit_daily_generation WHERE aux_consumption_mu > gross_gen_mu AND gross_gen_mu > 0"), 0)
    check("Rows where declared_capacity < gross_gen i.e. PAF<PLF (should be 0)", 0, scalar(
        "SELECT COUNT(*) FROM core_unit_daily_generation WHERE declared_capacity_mu < gross_gen_mu - 0.001"), 0)
    check("Rows with negative gross/aux (should be 0)", 0, scalar(
        "SELECT COUNT(*) FROM core_unit_daily_generation WHERE gross_gen_mu < 0 OR aux_consumption_mu < 0"), 0)
    return results
