"""Reconcile the loaded gms data against document_anchors.json.

Strategy per anchor, in dependency order (later steps assume earlier ones hold):

  1. RTPS/SGS daily reports (06-Apr-2025 / 06-Apr-2025) -- single-day, unit-level.
     Overwrite unit_daily_generation rows for that exact date with the document's
     unit figures directly. This is the most granular anchor: every later, coarser
     anchor (station-day, month, FY) is a sum that must still include this day, so
     fixing it first and holding it fixed avoids the rebalancer fighting itself.

  2. RTPS MPR March 2025 (month, per unit) and RTPS MPR FY 2024-25 (FY, per unit).
     These give unit-level MONTH and FY totals. The daily rows across the rest of
     the month/year are rescaled multiplicatively (preserving each day's shape --
     weekday/weekend/outage pattern) so they sum exactly to the anchor, while the
     already-fixed 06-Apr-2025 day (outside March) is untouched, and any day inside
     March that is separately anchored (there are none here) would be held fixed too.

  3. SGS MPR March 2025 (month, per unit) -- same rescaling approach.

  4. HO report per-station FY/last-FY/WY/last-WY totals (33 stations) -- coarser
     than the RTPS/SGS unit anchors above, so applied only to stations NOT already
     fixed by steps 2-3 at the unit level, and only for the FY/WY totals not
     already implied. Where a station has both a station-level FY anchor here and
     a unit-level FY anchor from step 2 (RTPS), the unit-level one wins (finer
     grain, already applied) and this step is a no-op / consistency check for it.

  5. Coal, dam levels, grid: point values on specific dates, applied directly.

  6. Verification: re-derive every anchor from the adjusted data and diff against
     document_anchors.json, writing reconciliation_report.json.

Rescaling detail (step 2/3): for a unit u with document total T over a period,
and current (pre-fix) daily values x_1..x_n summing to S:
    - if S > 0: new x_i = x_i * (T / S)  -- preserves relative shape exactly
    - if S == 0 (unit was at zero the whole period, e.g. RTPS-U1/U4 forced out):
      leave at zero UNLESS the document anchor is also ~0 (consistent) or the unit
      is documented as running (contradiction -- flagged, not silently invented).
All dependent columns (aux, coal, PLF-driving declared_capacity, heat rate inputs)
scale with gross_gen_mu multiplicatively where the relationship is proportional,
since only their DOCUMENT-STATED aggregate is anchored, not their daily split.
"""
import json
import sys

import psycopg2

DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@localhost:5432/gms"
ANCHORS = json.load(open("document_anchors.json", encoding="utf8"))

con = psycopg2.connect(DSN)
con.autocommit = False
cur = con.cursor()
cur.execute("SET search_path = gms")

log = []


def note(msg):
    print(msg)
    log.append(msg)


# ---------------------------------------------------------------------------
# STEP 1: single-day unit-level overwrites (RTPS + SGS, 06-Apr-2025)
# ---------------------------------------------------------------------------
def step1_daily_overwrite():
    note("\n=== STEP 1: RTPS & SGS daily report, 2025-04-06 ===")
    rtps = ANCHORS["rtps_daily_report_2025_04_06"]["units"]
    for u, v in rtps.items():
        unit_id = f"RTPS-{u}"
        if v.get("gen_mu") is None:
            # documented as force-tripped all day -- confirm current data agrees, don't invent generation
            cur.execute("SELECT gross_gen_mu FROM unit_daily_generation WHERE unit_id=%s AND gen_date='2025-04-06'", (unit_id,))
            row = cur.fetchone()
            if row and row[0] != 0:
                note(f"  {unit_id}: document shows outage (no gen), current data has {row[0]} MU -> zeroing")
                cur.execute("""UPDATE unit_daily_generation SET gross_gen_mu=0, aux_consumption_mu=0, coal_consumed_mt=0,
                               hfo_consumed_kl=0, running_hrs=0, forced_outage_hrs=24, peak_hour_gen_mu=0, peak_load_mw=0
                               WHERE unit_id=%s AND gen_date='2025-04-06'""", (unit_id,))
            continue
        cur.execute("""UPDATE unit_daily_generation SET
                        gross_gen_mu=%(gen)s, aux_consumption_mu=%(aux)s, peak_hour_gen_mu=%(peak)s,
                        peak_load_mw=%(load)s, running_hrs=%(run)s, forced_outage_hrs=%(fo)s,
                        coal_consumed_mt=%(coal)s, h2_cylinders_nos=%(h2)s
                       WHERE unit_id=%(uid)s AND gen_date='2025-04-06'""",
                    dict(gen=v["gen_mu"], aux=v["aux_mu"], peak=v.get("peak_hr_gen_mu"), load=v.get("day_peak_load_mw"),
                         run=v.get("running_hrs", 24), fo=24 - v.get("running_hrs", 24), coal=v.get("coal_consumed_mt"),
                         h2=v.get("h2_cylinders"), uid=unit_id))
    note("  RTPS units updated for 2025-04-06.")

    sgs = ANCHORS["sgs_daily_report_2025_04_06"]["units_mu"]
    for u, gen in sgs.items():
        unit_id = f"SGS-{u}"
        cur.execute("UPDATE unit_daily_generation SET gross_gen_mu=%s WHERE unit_id=%s AND gen_date='2025-04-06'", (gen, unit_id))
    note("  SGS units updated for 2025-04-06.")
    con.commit()


# ---------------------------------------------------------------------------
# STEP 2/3: rescale a unit's daily gross_gen_mu (and proportional columns) over
# a date range so the period sums to a target, preserving relative day-shape.
# ---------------------------------------------------------------------------
def rescale_unit_period(unit_id, date_from, date_to, target_mu, exclude_dates=(), label=""):
    exclude_clause = ""
    params = [unit_id, date_from, date_to]
    if exclude_dates:
        exclude_clause = "AND gen_date NOT IN %s"
        params.append(tuple(exclude_dates))
    cur.execute(f"""SELECT gen_date, gross_gen_mu, aux_consumption_mu, coal_consumed_mt, hfo_consumed_kl,
                            peak_hour_gen_mu
                     FROM unit_daily_generation WHERE unit_id=%s AND gen_date BETWEEN %s AND %s {exclude_clause}
                     ORDER BY gen_date""", params)
    rows = cur.fetchall()
    if not rows:
        note(f"  [{label}] {unit_id}: NO ROWS in range {date_from}..{date_to} -- skipped")
        return
    excluded_sum = 0.0
    if exclude_dates:
        cur.execute("SELECT COALESCE(SUM(gross_gen_mu),0) FROM unit_daily_generation WHERE unit_id=%s AND gen_date IN %s",
                    (unit_id, tuple(exclude_dates)))
        excluded_sum = float(cur.fetchone()[0])
    current_sum = sum(float(r[1]) for r in rows)
    remaining_target = target_mu - excluded_sum
    if remaining_target < 0:
        note(f"  [{label}] {unit_id}: WARNING target {target_mu} < already-fixed days sum {excluded_sum:.3f} -- clamping to 0")
        remaining_target = 0.0
    if current_sum <= 1e-9:
        if remaining_target <= 1e-9:
            note(f"  [{label}] {unit_id}: both current and target ~0 -- consistent, no change")
        else:
            note(f"  [{label}] {unit_id}: CONTRADICTION -- current sum is 0 but document target is {remaining_target:.3f} "
                 f"MU over {len(rows)} days; cannot rescale zeros. Needs a manual generation-loss profile, skipped.")
        return
    factor = remaining_target / current_sum
    for r in rows:
        gen_date = r[0]
        new_gross = float(r[1]) * factor
        new_aux = float(r[2]) * factor if r[2] else r[2]
        new_coal = float(r[3]) * factor if r[3] else r[3]
        new_hfo = float(r[4]) * factor if r[4] else r[4]
        new_peak = float(r[5]) * factor if r[5] else r[5]
        # declared_capacity_mu (PAF driver) and running/outage hours are NOT rescaled here: they are independent
        # of the day's actual output level. If gross_gen_mu now exceeds declared_capacity_mu for some day (only
        # possible if the day was scaled up a lot), lift declared_capacity_mu to match so PAF>=PLF stays true.
        cur.execute("""UPDATE unit_daily_generation SET gross_gen_mu=%s, aux_consumption_mu=%s, coal_consumed_mt=%s,
                       hfo_consumed_kl=%s, peak_hour_gen_mu=%s,
                       declared_capacity_mu = GREATEST(declared_capacity_mu, %s)
                       WHERE unit_id=%s AND gen_date=%s""",
                    (new_gross, new_aux, new_coal, new_hfo, new_peak, new_gross, unit_id, gen_date))
    note(f"  [{label}] {unit_id}: {len(rows)} days rescaled by x{factor:.4f} "
         f"(was {current_sum:.3f} MU, target {remaining_target:.3f} MU + {excluded_sum:.3f} fixed = {target_mu:.3f})")


def step2_rtps_month_fy():
    note("\n=== STEP 2: RTPS MPR March 2025 (unit-month) ===")
    units = ANCHORS["rtps_mpr_march_2025"]["units"]
    for u, v in units.items():
        if v.get("gross_gen_mu") is None:
            continue  # U1/U4: documented outage all month, handled by existing exceptions data
        unit_id = f"RTPS-{u}"
        excl = ["2025-04-06"] if unit_id in ("RTPS-U2", "RTPS-U3", "RTPS-U5", "RTPS-U6", "RTPS-U7", "RTPS-U8") else []
        # April 6 is outside March -- no actual overlap, exclude list only matters when ranges intersect
        rescale_unit_period(unit_id, "2025-03-01", "2025-03-31", v["gross_gen_mu"], exclude_dates=(), label="RTPS-MAR25")

    note("\n=== STEP 2b: RTPS MPR FY 2024-25 (unit-FY, excluding March which is separately pinned above) ===")
    fy_units = ANCHORS["rtps_mpr_fy_2024_25"]["units"]
    for u, v in fy_units.items():
        if v.get("gross_gen_mu") is None:
            continue
        unit_id = f"RTPS-{u}"
        # get March's now-fixed sum so the FY rescale only touches Apr'24-Feb'25 + Apr 6'25 falls outside FY24-25 anyway
        cur.execute("SELECT COALESCE(SUM(gross_gen_mu),0) FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date "
                    "WHERE unit_id=%s AND c.fy='2024-25' AND c.month_label='MAR-2025'", (unit_id,))
        march_fixed = float(cur.fetchone()[0])
        cur.execute("SELECT MIN(cal_date), MAX(cal_date) FROM dim_date WHERE fy='2024-25' AND month_label<>'MAR-2025'")
        # rescale only the non-March part of the FY to hit (FY target - march_fixed)
        cur.execute("""SELECT gen_date FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date
                       WHERE unit_id=%s AND c.fy='2024-25' AND c.month_label<>'MAR-2025' ORDER BY gen_date""", (unit_id,))
        other_dates = [r[0] for r in cur.fetchall()]
        if not other_dates:
            continue
        target_other = v["gross_gen_mu"] - march_fixed
        d_from, d_to = min(other_dates), max(other_dates)
        # exclude the March days that fall inside [d_from, d_to] isn't needed since d_from/d_to span excludes March
        # but FY range April->March wraps around March being excluded in the middle -- handle via two sub-ranges
        cur.execute("""SELECT gen_date, gross_gen_mu, aux_consumption_mu, coal_consumed_mt, hfo_consumed_kl,
                              peak_hour_gen_mu
                       FROM unit_daily_generation d JOIN dim_date c ON c.cal_date=d.gen_date
                       WHERE unit_id=%s AND c.fy='2024-25' AND c.month_label<>'MAR-2025' ORDER BY gen_date""", (unit_id,))
        rows = cur.fetchall()
        current_sum = sum(float(r[1]) for r in rows)
        if current_sum <= 1e-9:
            if target_other <= 1e-9:
                note(f"  [RTPS-FY24-25] {unit_id}: non-March already ~0, target also ~0 -- OK")
            else:
                note(f"  [RTPS-FY24-25] {unit_id}: CONTRADICTION -- non-March sum 0, target {target_other:.3f} -- skipped")
            continue
        factor = target_other / current_sum
        for r in rows:
            gd, g, a, c_, h, p = r
            new_gross = float(g) * factor
            cur.execute("""UPDATE unit_daily_generation SET gross_gen_mu=%s, aux_consumption_mu=%s, coal_consumed_mt=%s,
                           hfo_consumed_kl=%s, peak_hour_gen_mu=%s,
                           declared_capacity_mu = GREATEST(declared_capacity_mu, %s)
                           WHERE unit_id=%s AND gen_date=%s""",
                        (new_gross, float(a) * factor if a else a, float(c_) * factor if c_ else c_,
                         float(h) * factor if h else h, float(p) * factor if p else p, new_gross,
                         unit_id, gd))
        note(f"  [RTPS-FY24-25] {unit_id}: {len(rows)} non-March FY days rescaled x{factor:.4f} "
             f"(target FY total {v['gross_gen_mu']:.3f} = March {march_fixed:.3f} + rest {target_other:.3f})")
    con.commit()


def step3_sgs_month():
    note("\n=== STEP 3: SGS MPR March 2025 (unit-month) ===")
    for u in ANCHORS["sgs_mpr_march_2025"]["units"]:
        unit_id = f"SGS-U{u['unit_no']}"
        rescale_unit_period(unit_id, "2025-03-01", "2025-03-31", u["gen_month_mu"], label="SGS-MAR25")
    con.commit()


def step4_ho_station_fy_wy():
    note("\n=== STEP 4: HO report station FY / WY totals (stations not already unit-anchored) ===")
    already_unit_anchored = {"RTPS", "SGS"}
    ho = ANCHORS["ho_daily_generation_report"]["stations"]
    for station, vals in ho.items():
        if station in already_unit_anchored:
            continue
        fy_gen = vals[4]  # fy_gen_mu
        if fy_gen is None:
            continue
        cur.execute("""SELECT COALESCE(SUM(gross_gen_mu),0) FROM unit_daily_generation d JOIN unit u USING(unit_id)
                       JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=%s AND c.fy='2024-25'""", (station,))
        current = float(cur.fetchone()[0])
        if current <= 1e-9:
            if fy_gen <= 1e-9:
                continue
            note(f"  [HO-FY] {station}: current FY sum 0, target {fy_gen} -- CONTRADICTION, skipped")
            continue
        factor = fy_gen / current
        cur.execute("""UPDATE unit_daily_generation d SET gross_gen_mu = gross_gen_mu * %s,
                       aux_consumption_mu = aux_consumption_mu * %s,
                       coal_consumed_mt = COALESCE(coal_consumed_mt * %s, coal_consumed_mt),
                       hfo_consumed_kl = COALESCE(hfo_consumed_kl * %s, hfo_consumed_kl)
                       FROM unit u, dim_date c WHERE d.unit_id=u.unit_id AND c.cal_date=d.gen_date
                       AND u.station_code=%s AND c.fy='2024-25'""", (factor, factor, factor, factor, station))
        note(f"  [HO-FY] {station}: rescaled FY24-25 x{factor:.4f} (was {current:.3f}, target {fy_gen:.3f})")

        # water year (hydro stations): only rescale WY portion outside FY24-25 already touched above, i.e. WY24-25 = Jun'24-May'25
        # since WY spans two FYs, and we already fixed FY24-25 (Apr'24-Mar'25), only rescale Apr'25 (part of WY, outside FY24-25... )
        # -- WY 2024-25 = Jun-2024..May-2025; FY 2024-25 = Apr-2024..Mar-2025. Overlap = Jun-2024..Mar-2025 (already scaled above).
        # Remaining WY-only days = Apr-2025..May-2025 (partial, data ends Apr-2025). Rescale just that tail to hit WY total.
        wy_gen = vals[6]
        if wy_gen is None:
            continue
        cur.execute("""SELECT COALESCE(SUM(gross_gen_mu),0) FROM unit_daily_generation d JOIN unit u USING(unit_id)
                       JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=%s AND c.water_year='2024-25'""", (station,))
        wy_current = float(cur.fetchone()[0])
        cur.execute("""SELECT COALESCE(SUM(gross_gen_mu),0) FROM unit_daily_generation d JOIN unit u USING(unit_id)
                       JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=%s AND c.water_year='2024-25'
                       AND c.fy='2024-25'""", (station,))
        wy_fy_overlap = float(cur.fetchone()[0])
        tail_target = wy_gen - wy_fy_overlap
        cur.execute("""SELECT gen_date, gross_gen_mu FROM unit_daily_generation d JOIN unit u USING(unit_id)
                       JOIN dim_date c ON c.cal_date=d.gen_date WHERE u.station_code=%s AND c.water_year='2024-25'
                       AND c.fy<>'2024-25' ORDER BY gen_date""", (station,))
        tail_rows = cur.fetchall()
        if tail_rows:
            tail_current = sum(float(r[1]) for r in tail_rows)
            if tail_current > 1e-9 and tail_target > 0:
                tfactor = tail_target / tail_current
                for gd, g in tail_rows:
                    cur.execute("UPDATE unit_daily_generation SET gross_gen_mu=gross_gen_mu*%s WHERE unit_id IN "
                                "(SELECT unit_id FROM unit WHERE station_code=%s) AND gen_date=%s", (tfactor, station, gd))
                note(f"  [HO-WY-tail] {station}: {len(tail_rows)} WY-tail days (Apr'25) rescaled x{tfactor:.4f}")
    con.commit()


def main():
    step1_daily_overwrite()
    step2_rtps_month_fy()
    step3_sgs_month()
    # step4 (HO-report FY/WY rescale for non-RTPS/SGS stations) removed: the HO report's fy_gen_mu/last_fy_gen_mu
    # columns are FY2025-26-to-date / FY2024-25-to-date-one-year-ago (both ~6 days deep, since the report is
    # dated 06-Apr-2025), NOT full-FY totals -- confirmed by fy_gen_mu==monthly_gen_mu for every station (April
    # is FY month 1). The real full-FY2024-25 anchor for these stations is the GMS workbook's own `anchors` sheet
    # (e.g. BTPS 8181.451, YTPS 6015.925, NPH 4096.355 ...), which the loaded data already matches to the decimal
    # per the workbook's own `validation` sheet -- so no rescale is needed or correct here.
    open("reconcile_log.txt", "w", encoding="utf8").write("\n".join(log))
    print("\nDone. Log written to reconcile_log.txt. Run verify_reconciliation.py next.")


if __name__ == "__main__":
    main()
