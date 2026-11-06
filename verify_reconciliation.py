"""Independently re-derive every anchor in document_anchors.json from the live
database and diff against the document. Does NOT trust reconcile_to_document.py's
own log -- queries the database fresh, the same way an NL2SQL chatbot would.

usage: python verify_reconciliation.py "postgresql://postgres@localhost:5432/gms"
"""
import json
import sys

import psycopg2

DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@localhost:5432/gms"
A = json.load(open("document_anchors.json", encoding="utf8"))

con = psycopg2.connect(DSN)
cur = con.cursor()
cur.execute("SET search_path = gms")

results = []


def check(label, expected, actual, tol=0.01):
    if expected is None or actual is None:
        status = "SKIP (null)"
        diff = None
    else:
        diff = float(actual) - float(expected)
        status = "PASS" if abs(diff) <= tol else "FAIL"
    results.append({"label": label, "expected": expected, "actual": float(actual) if actual is not None else None,
                     "diff": diff, "status": status})


def scalar(sql, params=()):
    cur.execute(sql, params)
    r = cur.fetchone()
    return r[0] if r else None


# --- RTPS daily 2025-04-06, unit level
rtps_daily = A["rtps_daily_report_2025_04_06"]["units"]
for u, v in rtps_daily.items():
    if v.get("gen_mu") is None:
        continue
    actual = scalar("SELECT gross_gen_mu FROM unit_daily_generation WHERE unit_id=%s AND gen_date='2025-04-06'", (f"RTPS-{u}",))
    check(f"RTPS-{u} gross_gen 06-Apr-2025", v["gen_mu"], actual)

# --- RTPS station daily 06-Apr-2025
check("RTPS station gross_gen 06-Apr-2025",
      A["ho_daily_generation_report"]["stations"]["RTPS"][0],
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
             "WHERE u.station_code='RTPS' AND gen_date='2025-04-06'"))

# --- SGS daily 2025-04-06, unit + station
sgs_daily = A["sgs_daily_report_2025_04_06"]["units_mu"]
for u, gen in sgs_daily.items():
    actual = scalar("SELECT gross_gen_mu FROM unit_daily_generation WHERE unit_id=%s AND gen_date='2025-04-06'", (f"SGS-{u}",))
    check(f"SGS-{u} gross_gen 06-Apr-2025", gen, actual)
check("SGS station gross_gen 06-Apr-2025", A["sgs_daily_report_2025_04_06"]["station_gen_mu"],
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
             "WHERE u.station_code='SGS' AND gen_date='2025-04-06'"))

# --- RTPS MPR March 2025, unit + station
rtps_mar = A["rtps_mpr_march_2025"]["units"]
for u, v in rtps_mar.items():
    if v.get("gross_gen_mu") is None:
        continue
    actual = scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date "
                     "WHERE unit_id=%s AND c.month_label='MAR-2025'", (f"RTPS-{u}",))
    check(f"RTPS-{u} gross_gen MAR-2025", v["gross_gen_mu"], actual)
check("RTPS station gross_gen MAR-2025", A["rtps_mpr_march_2025"]["station"]["gross_gen_mu"],
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
             "JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code='RTPS' AND c.month_label='MAR-2025'"))

# --- RTPS MPR FY 2024-25, unit + station
rtps_fy = A["rtps_mpr_fy_2024_25"]["units"]
for u, v in rtps_fy.items():
    if v.get("gross_gen_mu") is None:
        continue
    actual = scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date "
                     "WHERE unit_id=%s AND c.fy='2024-25'", (f"RTPS-{u}",))
    check(f"RTPS-{u} gross_gen FY2024-25", v["gross_gen_mu"], actual)
check("RTPS station gross_gen FY2024-25", A["rtps_mpr_fy_2024_25"]["station"]["gross_gen_mu"],
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
             "JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code='RTPS' AND c.fy='2024-25'"))
# U1 should be exactly zero all FY (forced outage since before FY start)
check("RTPS-U1 gross_gen FY2024-25 (should be 0, forced out all year)", 0,
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date "
             "WHERE unit_id='RTPS-U1' AND c.fy='2024-25'"))

# --- SGS MPR March 2025, unit + station
sgs_mar = A["sgs_mpr_march_2025"]["units"]
for u in sgs_mar:
    unit_id = f"SGS-U{u['unit_no']}"
    actual = scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date "
                     "WHERE unit_id=%s AND c.month_label='MAR-2025'", (unit_id,))
    check(f"{unit_id} gross_gen MAR-2025", u["gen_month_mu"], actual)
check("SGS station gross_gen MAR-2025", A["sgs_mpr_march_2025"]["station_totals"]["gen_month_mu"],
      scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
             "JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code='SGS' AND c.month_label='MAR-2025'"))

# --- HO report: non-RTPS/SGS stations should still match the workbook's own FY2024-25 anchors (untouched, sanity check)
ho = A["ho_daily_generation_report"]["stations"]
workbook_anchors_fy2425 = {  # from KPCL_GMS_10yr.xlsx 'anchors' sheet, read at session start
    "BTPS": 8181.451, "YTPS": 6015.925, "YCCPP": 707.024, "BWTEP": 6.74, "NPH": 4096.355, "VUGPH": 1207.555,
    "MGHE": 443.609, "LPH": 178.93, "GERUSOPPA": 583.843, "SUPA": 646.78, "KODASALLI": 526.875, "KADRA": 532.268,
    "ALAMATTI": 630.111, "GHATPRABHA": 114.051, "BHADRA": 70.34, "MUNIRABAD": 122.455, "SHIVA": 107.835,
    "SHIMSHA": 31.104, "MALLAPUR": 44.542, "MALAPRABHA": 5.216, "KG-WEF": 5.458, "MANI": 28.169,
}
for station, expected in workbook_anchors_fy2425.items():
    actual = scalar("SELECT SUM(gross_gen_mu) FROM unit_daily_generation d JOIN unit u USING(unit_id) "
                     "JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=%s AND c.fy='2024-25'", (station,))
    check(f"{station} gross_gen FY2024-25 (workbook anchor, untouched)", expected, actual, tol=0.5)

# --- schema-level consistency checks (must hold after any rescale) ---
bad_hours = scalar("SELECT COUNT(*) FROM unit_daily_generation "
                    "WHERE ABS(running_hrs+forced_outage_hrs+planned_outage_hrs+rsd_hrs-24) > 0.06")
check("Rows where hours don't sum to 24 (should be 0)", 0, bad_hours, tol=0)
bad_aux = scalar("SELECT COUNT(*) FROM unit_daily_generation WHERE aux_consumption_mu > gross_gen_mu AND gross_gen_mu > 0")
check("Rows where aux > gross (should be 0)", 0, bad_aux, tol=0)
bad_paf = scalar("SELECT COUNT(*) FROM unit_daily_generation WHERE declared_capacity_mu < gross_gen_mu - 0.001")
check("Rows where declared_capacity < gross_gen i.e. PAF<PLF (should be 0)", 0, bad_paf, tol=0)
neg = scalar("SELECT COUNT(*) FROM unit_daily_generation WHERE gross_gen_mu < 0 OR aux_consumption_mu < 0")
check("Rows with negative gross/aux (should be 0)", 0, neg, tol=0)

con.close()

passed = sum(1 for r in results if r["status"] == "PASS")
failed = [r for r in results if r["status"] == "FAIL"]
skipped = sum(1 for r in results if r["status"].startswith("SKIP"))

print(f"{'label':55s} {'expected':>14s} {'actual':>14s} {'diff':>10s}  status")
print("-" * 105)
for r in results:
    exp = f"{r['expected']:.4f}" if isinstance(r['expected'], (int, float)) else str(r['expected'])
    act = f"{r['actual']:.4f}" if r['actual'] is not None else "—"
    dif = f"{r['diff']:+.4f}" if r['diff'] is not None else "—"
    print(f"{r['label']:55s} {exp:>14s} {act:>14s} {dif:>10s}  {r['status']}")

print(f"\n{passed} PASS, {len(failed)} FAIL, {skipped} SKIP, out of {len(results)} checks")
json.dump(results, open("reconciliation_report.json", "w", encoding="utf8"), indent=2)
print("Full report written to reconciliation_report.json")
if failed:
    sys.exit(1)
