"""Load KPCL_GMS_10yr.xlsx into the gms schema (gms_schema.sql).

usage:  python load_gms.py "postgresql://postgres@localhost:5432/gms" [KPCL_GMS_10yr.xlsx]

Loads masters, daily operations, outages/BTF, targets, coal, reservoirs and grid.
JMR, commercial and DMS tables are left empty: the workbook has no data for them.
"""
import io
import sys

import pandas as pd
import psycopg2

DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@localhost:5432/gms"
XLSX = sys.argv[2] if len(sys.argv) > 2 else "KPCL_GMS_10yr.xlsx"

COMPANIES = {
    "KPCL": "Karnataka Power Corporation Limited",
    "RPCL": "Raichur Power Corporation Limited",
    "KPCGPCL": "KPCL Gas Power Corporation Limited",
}
PLANT_TYPES = {"THERMAL": "Coal-fired thermal", "GAS": "Gas / combined cycle", "WTE": "Waste to energy",
               "HYDRO": "Hydro electric", "WIND": "Wind", "SOLAR": "Solar PV"}
# plant_category -> (plant_type, dashboard label, dashboard order)  -- HO "Abstract Generation Details" panel
CATEGORIES = {"THERMAL-COAL": ("THERMAL", "THERMAL-COAL", 1), "MAJOR HYDRO": ("HYDRO", "MAJOR HYDRO", 2),
              "MINOR HYDRO": ("HYDRO", "MINOR HYDRO", 3), "GAS": ("GAS", "GAS", 4), "WTE": ("WTE", "RES", 5),
              "SOLAR": ("SOLAR", "SOLAR", 6), "WIND": ("WIND", "WIND", 7)}
FUELS = {"COAL": "Coal", "GAS": "Natural gas", "MSW": "Municipal solid waste", "WATER": "Water (hydro)",
         "WIND": "Wind", "SUN": "Solar irradiation"}
BACKDOWN = [("QUALITY_COAL", "Quality coal"), ("COAL_STOCK_BUILD", "Coal-stock built"),
            ("SHEAR_PIN_FAILURE", "Shear pin failure"), ("UNIT_TRIP_LOSS", "Unit trip loss"),
            ("UNIT_STARTUP_LOSS", "Unit start up loss"), ("EQUIP_SYS_CONSTRAINT", "Equipment / system constraints"),
            ("ANNUAL_MAINTENANCE", "Annual maintenance loss"), ("NO_LOAD_DEMAND", "No load demand"),
            ("FLY_ASH_SLAG", "Fly ash / slag")]
OUTAGE_REASON_PLANT_TYPE = {"BOILER_TUBE_LEAK": "THERMAL", "BOILER_AUX": "THERMAL", "COAL_MILL": "THERMAL",
                            "FLAME_FAILURE": "THERMAL", "ASH_HANDLING": "THERMAL", "PENSTOCK_GATE": "HYDRO"}
SUPPLIERS = {"MCL": ("Mahanadi Coalfields Ltd", False), "SCCL": ("Singareni Collieries Company Ltd", False),
             "WCL": ("Western Coalfields Ltd", False), "IMPORTED": ("Imported coal", True)}


def code(s):
    """'EROSION - SOOT BLOWER' -> 'EROSION_SOOT_BLOWER'"""
    return "_".join(str(s).upper().replace("-", " ").split())


def copy(cur, table, df):
    # whole-number floats (e.g. ints with NaN) must go out as "154", not "154.0", for integer columns
    for col in df.columns:
        s = df[col]
        if s.dtype.kind == "f" and s.notna().any() and (s.dropna() % 1 == 0).all():
            df[col] = s.astype("Int64")
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    cur.copy_expert(f"COPY gms.{table} ({','.join(df.columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)
    print(f"  {table:28s} {len(df):>8,d}")


def main():
    print(f"reading {XLSX} ...")
    x = {s: pd.read_excel(XLSX, sheet_name=s) for s in (
        "projects", "units", "calendar", "reservoirs", "daily_unit_generation", "unit_outages", "generation_loss",
        "generation_targets", "coal_stock", "coal_receipts", "reservoir_levels", "daily_grid_summary")}

    con = psycopg2.connect(DSN)
    cur = con.cursor()
    print("loading ...")

    # ---- masters
    copy(cur, "company", pd.DataFrame(list(COMPANIES.items()), columns=["company_code", "company_name"]))
    copy(cur, "plant_type", pd.DataFrame(list(PLANT_TYPES.items()), columns=["plant_type_code", "description"]))
    copy(cur, "plant_category", pd.DataFrame(
        [(k, *v) for k, v in CATEGORIES.items()],
        columns=["plant_category_code", "plant_type_code", "dashboard_label", "dashboard_order"]))
    p = x["projects"]
    copy(cur, "station", pd.DataFrame({
        "station_code": p.project_code, "station_name": p.project_name, "company_code": p.company_code,
        "plant_category_code": p.plant_category, "installed_capacity_mw": p.installed_capacity_mw,
        "commissioning_date": p.commissioning_date.dt.date, "report_order": p.report_order, "is_active": p.is_active}))
    copy(cur, "fuel", pd.DataFrame(list(FUELS.items()), columns=["fuel_code", "description"]))
    u = x["units"]
    copy(cur, "unit", pd.DataFrame({
        "unit_id": u.unit_id, "station_code": u.project_code, "unit_no": u.unit_no, "capacity_mw": u.capacity_mw,
        "fuel_code": u.fuel_type, "technology": u.technology, "commissioning_date": u.commissioning_date.dt.date,
        "decommissioning_date": u.decommissioning_date.dt.date if u.decommissioning_date.notna().any() else None,
        "design_heat_rate_kcal_kwh": u.design_heat_rate_kcal_kwh, "opening_balance_date": u.opening_balance_date.dt.date,
        "opening_cum_gen_mu": u.opening_cum_gen_mu, "opening_cum_running_hrs": u.opening_cum_running_hrs,
        "opening_cum_trips": u.opening_cum_trips}))
    c = x["calendar"]
    copy(cur, "dim_date", pd.DataFrame({
        "cal_date": c.cal_date.dt.date, "fy": c.fy, "fy_start_date": c.fy_start.dt.date, "fy_month_no": c.fy_month_no,
        "water_year": c.water_year, "wy_month_no": c.wy_month_no, "month_start": c.month_start.dt.date,
        "year_month": c.cal_date.dt.strftime("%Y-%m"), "month_label": c.month_label, "fy_quarter": c.quarter,
        "week_start": c.week_start.dt.date, "day_name": c.day_name, "is_weekend": c.is_weekend,
        "is_holiday": c.is_holiday.fillna(False), "holiday_name": c.holiday_name, "season": c.season}))
    r = x["reservoirs"]
    copy(cur, "reservoir", pd.DataFrame({
        "reservoir_id": r.reservoir_id, "reservoir_name": r.reservoir_name, "level_unit": r.level_unit,
        "full_reservoir_level": r.full_reservoir_level, "min_draw_down_level": r.min_draw_down_level,
        "energy_at_frl_mu": r.energy_at_frl_mu}))
    rs = r[["reservoir_id", "feeds_projects"]].assign(station_code=r.feeds_projects.str.split(",")).explode("station_code")
    copy(cur, "reservoir_station", rs[["reservoir_id", "station_code"]])

    # ---- lookups
    copy(cur, "backdown_reason", pd.DataFrame(
        [(k, d, i + 1) for i, (k, d) in enumerate(BACKDOWN)], columns=["reason_code", "description", "display_order"]))
    o = x["unit_outages"]
    reasons = o[["reason_code", "reason_detail"]].drop_duplicates("reason_code")
    copy(cur, "outage_reason", pd.DataFrame({
        "reason_code": reasons.reason_code, "description": reasons.reason_code.str.replace("_", " ").str.title(),
        "plant_type_code": reasons.reason_code.map(OUTAGE_REASON_PLANT_TYPE)}))
    btf = o[o.is_btf == True]
    copy(cur, "btf_failure_type", pd.DataFrame({"failure_type_code": [code(v) for v in btf.btf_failure_type.unique()],
                                                "description": btf.btf_failure_type.unique()}))
    copy(cur, "btf_location", pd.DataFrame({"location_code": [code(v) for v in btf.btf_location.unique()],
                                            "description": btf.btf_location.unique()}))
    copy(cur, "coal_supplier", pd.DataFrame(
        [(k, n, i) for k, (n, i) in SUPPLIERS.items()], columns=["supplier_code", "supplier_name", "is_imported"]))

    # ---- daily generation (+ NLD deemed generation from the no-load-demand back-down)
    d, gl = x["daily_unit_generation"], x["generation_loss"]
    nld = (gl[gl.reason_code == "NO_LOAD_DEMAND"].groupby(["unit_id", "loss_date"]).loss_mu.sum()
           .rename("deemed_gen_mu").reset_index().rename(columns={"loss_date": "gen_date"}))
    d = d.merge(nld, on=["unit_id", "gen_date"], how="left")
    copy(cur, "unit_daily_generation", pd.DataFrame({
        "unit_id": d.unit_id, "gen_date": d.gen_date.dt.date, "gross_gen_mu": d.gross_gen_mu,
        "aux_consumption_mu": d.aux_consumption_mu, "peak_hour_gen_mu": d.peak_hour_gen_mu,
        "peak_load_mw": d.peak_load_mw, "running_hrs": d.running_hrs, "forced_outage_hrs": d.forced_outage_hrs,
        "planned_outage_hrs": d.planned_outage_hrs, "rsd_hrs": d.rsd_hrs, "trips": d.no_of_trips,
        "declared_capacity_mu": (d.paf_pct / 100 * d.capacity_mw * 24 / 1000).round(4),
        "deemed_gen_mu": d.deemed_gen_mu.fillna(0),
        "coal_consumed_mt": d.coal_consumed_mt, "hfo_consumed_kl": d.hfo_consumed_kl,
        "ldo_consumed_kl": d.ldo_consumed_kl, "gas_consumed_mmscm": d.gas_consumed_mmscm,
        "msw_consumed_mt": d.msw_consumed_mt, "gcv_coal_kcal_kg": d.gcv_coal_kcal_kg.where(d.gcv_coal_kcal_kg > 0),
        "gcv_oil_kcal_l": d.gcv_oil_kcal_l.where(d.gcv_oil_kcal_l > 0),
        "gross_heat_rate_kcal_kwh": d.gross_heat_rate_kcal_kwh.where(d.gross_heat_rate_kcal_kwh > 0),
        "dm_water_cum": d.dm_water_cum, "h2_cylinders_nos": d.h2_cylinders_nos,
        "water_used_mcft": d.water_used_mcft, "data_source": d.data_source}))
    copy(cur, "unit_generation_loss", pd.DataFrame({
        "unit_id": gl.unit_id, "loss_date": gl.loss_date.dt.date, "reason_code": gl.reason_code, "loss_mu": gl.loss_mu}))

    # ---- outages & BTF
    copy(cur, "unit_outage", pd.DataFrame({
        "outage_id": o.outage_id, "unit_id": o.unit_id, "outage_type": o.outage_type, "trip_at": o.trip_datetime,
        "sync_at": o.sync_datetime, "outage_hrs": o.outage_hrs.where(o.sync_datetime.notna()),
        "reason_code": o.reason_code, "reason_detail": o.reason_detail, "gen_loss_mu": o.gen_loss_mu,
        "is_structural": o.is_structural, "data_source": o.data_source}))
    copy(cur, "boiler_tube_failure", pd.DataFrame({
        "btf_id": btf.outage_id, "outage_id": btf.outage_id,
        "failure_type_code": btf.btf_failure_type.map(code), "location_code": btf.btf_location.map(code)}))

    t = x["generation_targets"]
    copy(cur, "generation_target", pd.DataFrame({
        "unit_id": t.unit_id, "target_month": t.target_month.dt.date, "cea_target_mu": t.cea_target_mu,
        "kerc_approved_mu": t.kerc_approved_mu, "internal_target_mu": t.internal_target_mu}))

    # ---- coal
    cr = x["coal_receipts"]
    copy(cur, "coal_receipt", pd.DataFrame({
        "receipt_id": cr.receipt_id, "station_code": cr.project_code, "receipt_date": cr.receipt_date.dt.date,
        "supplier_code": cr.supplier_code, "no_of_rakes": cr.no_of_rakes, "quantity_mt": cr.quantity_mt,
        "gcv_received_kcal_kg": cr.gcv_received_kcal_kg}))
    cs = x["coal_stock"]
    copy(cur, "station_coal_stock", pd.DataFrame({
        "station_code": cs.project_code, "stock_date": cs.stock_date.dt.date, "opening_stock_mt": cs.opening_stock_mt,
        "receipt_mt": cs.receipt_mt, "indigenous_consumed_mt": cs.indigenous_consumed_mt,
        "imported_consumed_mt": cs.imported_consumed_mt, "days_of_stock": cs.days_of_stock,
        "normative_days": cs.normative_days}))

    # ---- hydrology & grid
    rl = x["reservoir_levels"]
    copy(cur, "reservoir_daily_level", pd.DataFrame({
        "reservoir_id": rl.reservoir_id, "level_date": rl.level_date.dt.date, "water_level": rl.water_level,
        "capacity_pct": rl.capacity_pct, "inflow_mcft": rl.inflow_mcft, "discharge_power_mcft": rl.discharge_power_mcft,
        "spill_sluice_mcft": rl.spill_sluice_mcft, "rainfall_mm": rl.rainfall_mm,
        "energy_potential_mu": rl.energy_potential_mu}))
    g = x["daily_grid_summary"]
    copy(cur, "state_grid_daily", pd.DataFrame({
        "summary_date": g.summary_date.dt.date, "state_consumption_mu": g.state_consumption_mu,
        "state_generation_mu": g.state_generation_mu, "state_import_mu": g.state_import_mu,
        "system_peak_mw": g.system_peak_mw, "freq_max_hz": g.freq_max_hz, "freq_min_hz": g.freq_min_hz,
        "tb_share_mu": g.tb_share_mu, "jurala_share_mu": g.jurala_share_mu}))

    con.commit()
    cur.execute("ANALYZE")
    con.close()
    print("done")


if __name__ == "__main__":
    main()
