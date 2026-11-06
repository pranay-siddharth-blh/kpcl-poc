"""
KPCL GMS warehouse (SQLite) — built from KPCL_GMS_10yr.xlsx and document_anchors.json.

Layers, one SQLite file, table-name prefix = layer (WB POC layout):
  src_*       registry of every source file and sheet; every cell of the small / irregular sheets
  ref_*       reference data read from documents (document_anchors.json flattened to rows)
  raw_*       one TEXT table per tabular sheet, value-for-value, with the Excel row number
  stg_*       typed copy of each raw_* table + quality_flags (JSON list) per row
  core_*      normalised model: SQLite translation of gms_schema.sql (schema_core.sql)
  mart_*      wide, query-ready dimensions and facts for the chatbot (mart.sql)
  semantic_*  table/column documentation, metric registry, capability manifest, data period
  ledger_*    lineage (file, column), known issues, reconciliation log and checks, build runs

Stages (run in order; --from resumes):
  sources  raw  staging  core  reconcile  mart  semantic  ledger  checks

Usage (from this folder):
  python build_warehouse.py                      # full build into kpcl_gms.db
  python build_warehouse.py --from mart          # rebuild mart and everything after it
  python build_warehouse.py --db other.db
The source workbook, the anchors JSON and the PostgreSQL files beside them are only read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import sqlite3
import sys
import time

import openpyxl
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import reconcile  # noqa: E402

WORKBOOK = ROOT / "KPCL_GMS_10yr.xlsx"
ANCHORS = ROOT / "document_anchors.json"
METADATA = HERE / "metadata"
DEFAULT_DB = HERE / "kpcl_gms.db"
DATA_AS_OF = "2025-04-30"          # last day in the workbook; relative dates resolve against it

STAGES = ["sources", "raw", "staging", "core", "reconcile", "mart", "semantic", "ledger", "checks"]

# Sheets kept cell by cell (small or irregular layout).
CELL_SHEETS = ["README", "YoY_check", "validation", "anchors", "exceptions", "projects", "units", "reservoirs"]
# Sheets kept as row tables (header on row 1).
ROW_SHEETS = ["validation", "anchors", "exceptions", "projects", "units", "reservoirs", "calendar",
              "annual_station_summary", "annual_unit_summary", "monthly_station_summary", "monthly_unit_summary",
              "daily_unit_generation", "unit_outages", "generation_loss", "generation_targets", "coal_stock",
              "coal_receipts", "reservoir_levels", "daily_grid_summary"]
LOADED_SHEETS = ["projects", "units", "calendar", "reservoirs", "daily_unit_generation", "unit_outages",
                 "generation_loss", "generation_targets", "coal_stock", "coal_receipts", "reservoir_levels",
                 "daily_grid_summary"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def as_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    return str(v)


def snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_") or "col"


def drop_prefixed(con: sqlite3.Connection, prefixes: tuple[str, ...]) -> None:
    rows = con.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    for kind, name in rows:
        if name.startswith(prefixes):
            con.execute(f"DROP {kind.upper()} IF EXISTS {name}")
    con.commit()


# ============================================================================= sources
def stage_sources(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("src_", "ref_"))
    con.executescript("""
    CREATE TABLE src_file (
        file_name TEXT PRIMARY KEY, file_role TEXT NOT NULL, loaded INTEGER NOT NULL, size_bytes INTEGER,
        sha256 TEXT, note TEXT);
    CREATE TABLE src_sheet (
        file_name TEXT NOT NULL, sheet_name TEXT NOT NULL, sheet_order INTEGER, max_row INTEGER, max_col INTEGER,
        storage TEXT NOT NULL, raw_table TEXT, loaded_into_core INTEGER NOT NULL, note TEXT,
        PRIMARY KEY (file_name, sheet_name));
    CREATE TABLE src_cell (
        file_name TEXT NOT NULL, sheet_name TEXT NOT NULL, cell_ref TEXT NOT NULL, row_no INTEGER NOT NULL,
        col_no INTEGER NOT NULL, value TEXT, formula TEXT, PRIMARY KEY (file_name, sheet_name, row_no, col_no));
    CREATE TABLE ref_document_anchor (
        anchor_path TEXT PRIMARY KEY, section TEXT NOT NULL, value_text TEXT, value_num REAL);
    """)
    files = [
        ("KPCL_GMS_10yr.xlsx", "primary synthetic dataset (21 sheets)", 1,
         "Loaded: 12 sheets into core; all tabular sheets mirrored in raw_*."),
        ("document_anchors.json", "real figures transcribed from the K-eRP document", 1,
         "Loaded as ref_document_anchor; drives the reconcile stage."),
        ("erp_at_kpcl_hrms.docx", "source document (K-eRP screenshots)", 0,
         "Reference only; its figures reach the warehouse through document_anchors.json."),
        ("KPCL_33_Projects_10Y_Joint_Meter_and_MPR.xlsx", "second synthetic workbook (monthly JMR + MPR)", 0,
         "Not loaded: different station names/capacities and generation levels from the primary workbook."),
        ("gms_schema.sql", "PostgreSQL design of the core model", 0, "Translated to warehouse/schema_core.sql."),
        ("gms_mart.sql", "PostgreSQL design of the star-schema views", 0, "Re-implemented as warehouse/mart.sql."),
    ]
    for name, role, loaded, note in files:
        p = ROOT / name
        con.execute("INSERT INTO src_file VALUES (?,?,?,?,?,?)",
                    (name, role, loaded, p.stat().st_size if p.exists() else None, sha256(p) if p.exists() else None, note))

    log("reading small sheets cell by cell ...")
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=False)
    for order, ws in enumerate(wb.worksheets, 1):
        name = ws.title
        storage = "cell_grid" if name in CELL_SHEETS else ("row_table" if name in ROW_SHEETS else "none")
        if name in CELL_SHEETS and name in ROW_SHEETS:
            storage = "cell_grid+row_table"
        con.execute("INSERT INTO src_sheet VALUES (?,?,?,?,?,?,?,?,?)", (
            WORKBOOK.name, name, order, ws.max_row, ws.max_column, storage,
            f"raw_{snake(name)}" if name in ROW_SHEETS else None, int(name in LOADED_SHEETS),
            None if name in LOADED_SHEETS else "not loaded into core (provenance / roll-up of the daily sheet)"))
        if name in CELL_SHEETS:
            batch = []
            for row in ws.iter_rows():
                for c in row:
                    if c.value is None:
                        continue
                    v = c.value
                    formula = v if isinstance(v, str) and v.startswith("=") else None
                    batch.append((WORKBOOK.name, name, c.coordinate, c.row, c.column,
                                  None if formula else as_text(v), formula))
            con.executemany("INSERT INTO src_cell VALUES (?,?,?,?,?,?,?)", batch)
    wb.close()

    anchors = json.loads(ANCHORS.read_text(encoding="utf8"))

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{path}[{i}]")
        else:
            num = obj if isinstance(obj, (int, float)) and not isinstance(obj, bool) else None
            yield path, path.split(".")[0], None if obj is None else str(obj), num
    con.executemany("INSERT INTO ref_document_anchor VALUES (?,?,?,?)", list(walk(anchors, "")))
    con.commit()
    log(f"sources: {con.execute('SELECT COUNT(*) FROM src_cell').fetchone()[0]:,} cells, "
        f"{con.execute('SELECT COUNT(*) FROM ref_document_anchor').fetchone()[0]:,} anchor values")


# ============================================================================= raw
def stage_raw(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("raw_",))
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=False)
    for name in ROW_SHEETS:
        t0 = time.time()
        ws = wb[name]
        rows = ws.iter_rows(values_only=True)
        header = [snake(h) for h in next(rows)]
        cols = ", ".join(f'"{h}" TEXT' for h in header)
        table = f"raw_{snake(name)}"
        con.execute(f"CREATE TABLE {table} (source_row_no INTEGER PRIMARY KEY, {cols})")
        sql = f"INSERT INTO {table} VALUES ({','.join('?' * (len(header) + 1))})"
        batch, n = [], 0
        for i, r in enumerate(rows, start=2):
            if all(v is None for v in r):
                continue
            r = list(r)[: len(header)] + [None] * (len(header) - len(r))
            batch.append((i, *[as_text(v) for v in r]))
            if len(batch) >= 20000:
                con.executemany(sql, batch)
                n += len(batch)
                batch = []
        con.executemany(sql, batch)
        n += len(batch)
        con.commit()
        log(f"raw: {table:32s} {n:>8,d} rows ({time.time() - t0:.0f}s)")
    wb.close()


# ============================================================================= staging
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( 00:00:00)?$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$")
_INT_NAME_RE = re.compile(r"(^|_)(id|no|nos|trips|order|days|rakes|month_no|count)$|^no_of_|_id$|^outage_id$")


def _infer(series: pd.Series, name: str) -> str:
    s = series.dropna()
    if s.empty:
        return "text"
    if s.isin(["True", "False"]).all():
        return "bool"
    if s.str.match(_DATE_RE).all():
        return "date"
    if s.str.match(_DATETIME_RE).all():
        return "datetime"
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().all():
        if _INT_NAME_RE.search(name) and (num % 1 == 0).all():
            return "int"
        return "real"
    return "text"


def _quality_flags(sheet: str, df: pd.DataFrame) -> pd.Series:
    flags = [[] for _ in range(len(df))]

    def add(mask, flag):
        for i in mask[mask.fillna(False)].index:
            flags[df.index.get_loc(i)].append(flag)

    if sheet == "daily_unit_generation":
        add(df.gcv_coal_kcal_kg <= 0, "gcv_coal_nonpositive_nulled")
        add(df.gcv_oil_kcal_l <= 0, "gcv_oil_nonpositive_nulled")
        add(df.gross_heat_rate_kcal_kwh <= 0, "heat_rate_nonpositive_nulled")
        add(df.gross_heat_rate_kcal_kwh > 5000, "heat_rate_outlier_low_generation")
        add(df.sp_oil_ml_kwh > 50, "sp_oil_outlier_low_generation")
        add(df.paf_pct < df.plf_pct - 0.001, "paf_below_plf_rounding")
    elif sheet == "unit_outages":
        add(df.sync_datetime.isna(), "open_outage")
        add(df.event_source != "Synthetic", "documented_event")
    elif sheet == "generation_targets":
        add(df.kerc_approved_mu.isna(), "kerc_target_missing")
    elif sheet == "reservoir_levels":
        add(df.energy_potential_mu.isna(), "energy_potential_missing")
    return pd.Series([json.dumps(f) if f else None for f in flags], index=df.index)


def stage_staging(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("stg_",))
    for name in ROW_SHEETS:
        t0 = time.time()
        raw = f"raw_{snake(name)}"
        df = pd.read_sql(f"SELECT * FROM {raw}", con)
        types = {}
        for c in df.columns:
            if c == "source_row_no":
                continue
            kind = _infer(df[c], c)
            types[c] = kind
            if kind == "bool":
                df[c] = df[c].map({"True": 1, "False": 0}).astype("Int64")
            elif kind == "date":
                df[c] = df[c].str[:10]
            elif kind == "datetime":
                df[c] = df[c].str[:19]
            elif kind == "int":
                df[c] = pd.to_numeric(df[c]).astype("Int64")
            elif kind == "real":
                df[c] = pd.to_numeric(df[c])
        df["quality_flags"] = _quality_flags(name, df)
        sqltype = {"bool": "INTEGER", "int": "INTEGER", "real": "REAL", "date": "TEXT", "datetime": "TEXT", "text": "TEXT"}
        cols = ", ".join(f'"{c}" {sqltype[types[c]]}' for c in types)
        con.execute(f"CREATE TABLE stg_{snake(name)} (source_row_no INTEGER PRIMARY KEY, {cols}, quality_flags TEXT)")
        df.to_sql(f"stg_{snake(name)}", con, if_exists="append", index=False, chunksize=20000)
        con.execute("CREATE TABLE IF NOT EXISTS stg__column_type (stg_table TEXT, column_name TEXT, inferred_type TEXT, "
                    "PRIMARY KEY (stg_table, column_name))")
        con.executemany("INSERT OR REPLACE INTO stg__column_type VALUES (?,?,?)",
                        [(f"stg_{snake(name)}", c, k) for c, k in types.items()])
        con.commit()
        flagged = int(df.quality_flags.notna().sum())
        log(f"staging: stg_{snake(name):28s} {len(df):>8,d} rows, {flagged:,} flagged ({time.time() - t0:.0f}s)")


# ============================================================================= core
COMPANIES = {"KPCL": "Karnataka Power Corporation Limited", "RPCL": "Raichur Power Corporation Limited",
             "KPCGPCL": "KPCL Gas Power Corporation Limited"}
PLANT_TYPES = {"THERMAL": "Coal-fired thermal", "GAS": "Gas / combined cycle", "WTE": "Waste to energy",
               "HYDRO": "Hydro electric", "WIND": "Wind", "SOLAR": "Solar PV"}
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
SUPPLIERS = {"MCL": ("Mahanadi Coalfields Ltd", 0), "SCCL": ("Singareni Collieries Company Ltd", 0),
             "WCL": ("Western Coalfields Ltd", 0), "IMPORTED": ("Imported coal", 1)}

# Lineage: which staging column feeds which core column (read by the ledger stage).
LINEAGE: dict[str, dict[str, str]] = {}


def _code(s: str) -> str:
    """'EROSION - SOOT BLOWER' -> 'EROSION_SOOT_BLOWER' (same rule as load_gms.py)."""
    return "_".join(str(s).upper().replace("-", " ").split())


def _insert(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> None:
    df = df.astype(object).where(df.notna(), None)
    cols = list(df.columns)
    con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    df.itertuples(index=False, name=None))
    log(f"core: {table:32s} {len(df):>8,d}")


def _map(sheet: str, core_table: str, pairs: dict[str, str]) -> None:
    for stg_col, core_col in pairs.items():
        plain = core_col.startswith(("UNMAPPED", "core_"))
        LINEAGE.setdefault(sheet, {})[stg_col] = core_col if plain else f"{core_table}.{core_col}"


def stage_core(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("core_",))
    con.executescript((HERE / "schema_core.sql").read_text(encoding="utf8"))
    con.execute("PRAGMA foreign_keys = ON")
    s = {n: pd.read_sql(f"SELECT * FROM stg_{snake(n)}", con) for n in LOADED_SHEETS}

    _insert(con, "core_company", pd.DataFrame(list(COMPANIES.items()), columns=["company_code", "company_name"]))
    _insert(con, "core_plant_type", pd.DataFrame(list(PLANT_TYPES.items()), columns=["plant_type_code", "description"]))
    _insert(con, "core_plant_category", pd.DataFrame(
        [(k, *v) for k, v in CATEGORIES.items()],
        columns=["plant_category_code", "plant_type_code", "dashboard_label", "dashboard_order"]))

    p = s["projects"]
    _insert(con, "core_station", pd.DataFrame({
        "station_code": p.project_code, "station_name": p.project_name, "company_code": p.company_code,
        "plant_category_code": p.plant_category, "installed_capacity_mw": p.installed_capacity_mw,
        "commissioning_date": p.commissioning_date, "report_order": p.report_order, "is_active": p.is_active}))
    _map("projects", "core_station", {"project_code": "station_code", "project_name": "station_name",
                                      "company_code": "company_code", "plant_category": "plant_category_code",
                                      "installed_capacity_mw": "installed_capacity_mw",
                                      "commissioning_date": "commissioning_date", "report_order": "report_order",
                                      "is_active": "is_active", "plant_type": "core_plant_category.plant_type_code (via category)"})
    _insert(con, "core_fuel", pd.DataFrame(list(FUELS.items()), columns=["fuel_code", "description"]))

    u = s["units"]
    _insert(con, "core_unit", pd.DataFrame({
        "unit_id": u.unit_id, "station_code": u.project_code, "unit_no": u.unit_no, "capacity_mw": u.capacity_mw,
        "fuel_code": u.fuel_type, "technology": u.technology, "commissioning_date": u.commissioning_date,
        "decommissioning_date": u.decommissioning_date, "design_heat_rate_kcal_kwh": u.design_heat_rate_kcal_kwh,
        "opening_balance_date": u.opening_balance_date, "opening_cum_gen_mu": u.opening_cum_gen_mu,
        "opening_cum_running_hrs": u.opening_cum_running_hrs, "opening_cum_trips": u.opening_cum_trips}))
    _map("units", "core_unit", {c: c for c in ["unit_id", "unit_no", "capacity_mw", "technology", "commissioning_date",
                                               "decommissioning_date", "design_heat_rate_kcal_kwh", "opening_balance_date",
                                               "opening_cum_gen_mu", "opening_cum_running_hrs", "opening_cum_trips"]}
         | {"project_code": "station_code", "fuel_type": "fuel_code"})

    c = s["calendar"]
    _insert(con, "core_dim_date", pd.DataFrame({
        "cal_date": c.cal_date, "fy": c.fy, "fy_start_date": c.fy_start, "fy_month_no": c.fy_month_no,
        "water_year": c.water_year, "wy_month_no": c.wy_month_no, "month_start": c.month_start,
        "year_month": c.cal_date.str[:7], "month_label": c.month_label, "fy_quarter": c.quarter,
        "week_start": c.week_start, "day_name": c.day_name, "is_weekend": c.is_weekend,
        "is_holiday": c.is_holiday.fillna(0), "holiday_name": c.holiday_name, "season": c.season}))
    _map("calendar", "core_dim_date", {c_: c_ for c_ in ["cal_date", "fy", "fy_month_no", "water_year", "wy_month_no",
                                                         "month_start", "month_label", "week_start", "day_name",
                                                         "is_weekend", "is_holiday", "holiday_name", "season"]}
         | {"fy_start": "fy_start_date", "quarter": "fy_quarter"})

    r = s["reservoirs"]
    _insert(con, "core_reservoir", pd.DataFrame({
        "reservoir_id": r.reservoir_id, "reservoir_name": r.reservoir_name, "level_unit": r.level_unit,
        "full_reservoir_level": r.full_reservoir_level, "min_draw_down_level": r.min_draw_down_level,
        "energy_at_frl_mu": r.energy_at_frl_mu}))
    rs = r[["reservoir_id", "feeds_projects"]].assign(station_code=r.feeds_projects.str.split(",")).explode("station_code")
    _insert(con, "core_reservoir_station", rs[["reservoir_id", "station_code"]])
    _map("reservoirs", "core_reservoir", {c_: c_ for c_ in ["reservoir_id", "reservoir_name", "level_unit",
                                                            "full_reservoir_level", "min_draw_down_level", "energy_at_frl_mu"]}
         | {"feeds_projects": "core_reservoir_station.station_code (split on comma)"})

    _insert(con, "core_backdown_reason", pd.DataFrame(
        [(k, d, i + 1) for i, (k, d) in enumerate(BACKDOWN)], columns=["reason_code", "description", "display_order"]))
    o = s["unit_outages"]
    reasons = o[["reason_code"]].drop_duplicates()
    _insert(con, "core_outage_reason", pd.DataFrame({
        "reason_code": reasons.reason_code, "description": reasons.reason_code.str.replace("_", " ").str.title(),
        "plant_type_code": reasons.reason_code.map(OUTAGE_REASON_PLANT_TYPE)}))
    btf = o[o.is_btf == 1]
    _insert(con, "core_btf_failure_type", pd.DataFrame({
        "failure_type_code": [_code(v) for v in btf.btf_failure_type.unique()], "description": btf.btf_failure_type.unique()}))
    _insert(con, "core_btf_location", pd.DataFrame({
        "location_code": [_code(v) for v in btf.btf_location.unique()], "description": btf.btf_location.unique()}))
    _insert(con, "core_coal_supplier", pd.DataFrame(
        [(k, n, i) for k, (n, i) in SUPPLIERS.items()], columns=["supplier_code", "supplier_name", "is_imported"]))

    d, gl = s["daily_unit_generation"], s["generation_loss"]
    nld = (gl[gl.reason_code == "NO_LOAD_DEMAND"].groupby(["unit_id", "loss_date"]).loss_mu.sum()
           .rename("deemed_gen_mu").reset_index().rename(columns={"loss_date": "gen_date"}))
    d = d.merge(nld, on=["unit_id", "gen_date"], how="left")
    _insert(con, "core_unit_daily_generation", pd.DataFrame({
        "unit_id": d.unit_id, "gen_date": d.gen_date, "gross_gen_mu": d.gross_gen_mu,
        "aux_consumption_mu": d.aux_consumption_mu, "peak_hour_gen_mu": d.peak_hour_gen_mu,
        "peak_load_mw": d.peak_load_mw, "running_hrs": d.running_hrs, "forced_outage_hrs": d.forced_outage_hrs,
        "planned_outage_hrs": d.planned_outage_hrs, "rsd_hrs": d.rsd_hrs, "trips": d.no_of_trips,
        # PAF is stored to 2 decimals in the workbook, so on a few days it rounds below PLF; availability can never be
        # below actual output, so declared capacity is lifted to at least the day's gross (ledger issue KI-07).
        "declared_capacity_mu": (d.paf_pct / 100 * d.capacity_mw * 24 / 1000).round(4).clip(lower=d.gross_gen_mu),
        "deemed_gen_mu": d.deemed_gen_mu.fillna(0),
        "coal_consumed_mt": d.coal_consumed_mt, "hfo_consumed_kl": d.hfo_consumed_kl,
        "ldo_consumed_kl": d.ldo_consumed_kl, "gas_consumed_mmscm": d.gas_consumed_mmscm,
        "msw_consumed_mt": d.msw_consumed_mt, "gcv_coal_kcal_kg": d.gcv_coal_kcal_kg.where(d.gcv_coal_kcal_kg > 0),
        "gcv_oil_kcal_l": d.gcv_oil_kcal_l.where(d.gcv_oil_kcal_l > 0),
        "gross_heat_rate_kcal_kwh": d.gross_heat_rate_kcal_kwh.where(d.gross_heat_rate_kcal_kwh > 0),
        "dm_water_cum": d.dm_water_cum, "h2_cylinders_nos": d.h2_cylinders_nos,
        "water_used_mcft": d.water_used_mcft, "data_source": d.data_source}))
    same = ["unit_id", "gen_date", "gross_gen_mu", "aux_consumption_mu", "peak_hour_gen_mu", "peak_load_mw", "running_hrs",
            "forced_outage_hrs", "planned_outage_hrs", "rsd_hrs", "coal_consumed_mt", "hfo_consumed_kl", "ldo_consumed_kl",
            "gas_consumed_mmscm", "msw_consumed_mt", "gcv_coal_kcal_kg", "gcv_oil_kcal_l", "gross_heat_rate_kcal_kwh",
            "dm_water_cum", "h2_cylinders_nos", "water_used_mcft", "data_source"]
    _map("daily_unit_generation", "core_unit_daily_generation", {c_: c_ for c_ in same} | {
        "no_of_trips": "trips", "paf_pct": "declared_capacity_mu (= paf_pct/100 x capacity_mw x 24/1000)",
        "capacity_mw": "UNMAPPED (repeats core_unit.capacity_mw)", "project_code": "UNMAPPED (repeats core_unit.station_code)",
        "net_export_mu": "UNMAPPED (generated column: gross - aux)", "plf_pct": "UNMAPPED (recomputed in mart)",
        "sp_coal_kg_kwh": "UNMAPPED (recomputed in mart)", "sp_oil_ml_kwh": "UNMAPPED (recomputed in mart)"})

    _insert(con, "core_unit_generation_loss", pd.DataFrame({
        "unit_id": gl.unit_id, "loss_date": gl.loss_date, "reason_code": gl.reason_code, "loss_mu": gl.loss_mu}))
    _map("generation_loss", "core_unit_generation_loss", {"unit_id": "unit_id", "loss_date": "loss_date",
                                                          "reason_code": "reason_code", "loss_mu": "loss_mu",
                                                          "project_code": "UNMAPPED (repeats core_unit.station_code)",
                                                          "data_source": "UNMAPPED (always SYNTHETIC)"})

    _insert(con, "core_unit_outage", pd.DataFrame({
        "outage_id": o.outage_id, "unit_id": o.unit_id, "outage_type": o.outage_type, "trip_at": o.trip_datetime,
        "sync_at": o.sync_datetime, "outage_hrs": o.outage_hrs.where(o.sync_datetime.notna()),
        "reason_code": o.reason_code, "reason_detail": o.reason_detail, "gen_loss_mu": o.gen_loss_mu,
        "is_structural": o.is_structural, "data_source": o.data_source}))
    _insert(con, "core_boiler_tube_failure", pd.DataFrame({
        "btf_id": btf.outage_id, "outage_id": btf.outage_id,
        "failure_type_code": btf.btf_failure_type.map(_code), "location_code": btf.btf_location.map(_code)}))
    _map("unit_outages", "core_unit_outage", {c_: c_ for c_ in ["outage_id", "unit_id", "outage_type", "outage_hrs",
                                                                "reason_code", "reason_detail", "gen_loss_mu",
                                                                "is_structural", "data_source"]}
         | {"trip_datetime": "trip_at", "sync_datetime": "sync_at", "is_btf": "core_boiler_tube_failure (row exists)",
            "btf_failure_type": "core_boiler_tube_failure.failure_type_code", "btf_location": "core_boiler_tube_failure.location_code",
            "project_code": "UNMAPPED (repeats core_unit.station_code)", "event_source": "UNMAPPED (kept in stg quality_flags)"})

    t = s["generation_targets"]
    _insert(con, "core_generation_target", pd.DataFrame({
        "unit_id": t.unit_id, "target_month": t.target_month, "cea_target_mu": t.cea_target_mu,
        "kerc_approved_mu": t.kerc_approved_mu, "internal_target_mu": t.internal_target_mu}))
    _map("generation_targets", "core_generation_target", {c_: c_ for c_ in ["unit_id", "target_month", "cea_target_mu",
                                                                            "kerc_approved_mu", "internal_target_mu"]}
         | {"fy": "UNMAPPED (derived from core_dim_date)", "project_code": "UNMAPPED (repeats core_unit.station_code)"})

    cr = s["coal_receipts"]
    _insert(con, "core_coal_receipt", pd.DataFrame({
        "receipt_id": cr.receipt_id, "station_code": cr.project_code, "receipt_date": cr.receipt_date,
        "supplier_code": cr.supplier_code, "no_of_rakes": cr.no_of_rakes, "quantity_mt": cr.quantity_mt,
        "gcv_received_kcal_kg": cr.gcv_received_kcal_kg}))
    _map("coal_receipts", "core_coal_receipt", {c_: c_ for c_ in ["receipt_id", "receipt_date", "supplier_code",
                                                                  "no_of_rakes", "quantity_mt", "gcv_received_kcal_kg"]}
         | {"project_code": "station_code", "data_source": "UNMAPPED (always SYNTHETIC)"})

    cs = s["coal_stock"]
    _insert(con, "core_station_coal_stock", pd.DataFrame({
        "station_code": cs.project_code, "stock_date": cs.stock_date, "opening_stock_mt": cs.opening_stock_mt,
        "receipt_mt": cs.receipt_mt, "indigenous_consumed_mt": cs.indigenous_consumed_mt,
        "imported_consumed_mt": cs.imported_consumed_mt, "days_of_stock": cs.days_of_stock,
        "normative_days": cs.normative_days}))
    _map("coal_stock", "core_station_coal_stock", {c_: c_ for c_ in ["stock_date", "opening_stock_mt", "receipt_mt",
                                                                     "indigenous_consumed_mt", "imported_consumed_mt",
                                                                     "days_of_stock", "normative_days"]}
         | {"project_code": "station_code", "total_consumed_mt": "total_consumed_mt (generated column)",
            "closing_stock_mt": "closing_stock_mt (generated column)", "data_source": "UNMAPPED (always SYNTHETIC)"})

    rl = s["reservoir_levels"]
    _insert(con, "core_reservoir_daily_level", pd.DataFrame({
        "reservoir_id": rl.reservoir_id, "level_date": rl.level_date, "water_level": rl.water_level,
        "capacity_pct": rl.capacity_pct, "inflow_mcft": rl.inflow_mcft, "discharge_power_mcft": rl.discharge_power_mcft,
        "spill_sluice_mcft": rl.spill_sluice_mcft, "rainfall_mm": rl.rainfall_mm,
        "energy_potential_mu": rl.energy_potential_mu}))
    _map("reservoir_levels", "core_reservoir_daily_level", {c_: c_ for c_ in rl.columns if c_ not in (
        "source_row_no", "quality_flags", "data_source")} | {"data_source": "UNMAPPED (always SYNTHETIC)"})

    g = s["daily_grid_summary"]
    _insert(con, "core_state_grid_daily", pd.DataFrame({
        "summary_date": g.summary_date, "state_consumption_mu": g.state_consumption_mu,
        "state_generation_mu": g.state_generation_mu, "state_import_mu": g.state_import_mu,
        "system_peak_mw": g.system_peak_mw, "freq_max_hz": g.freq_max_hz, "freq_min_hz": g.freq_min_hz,
        "tb_share_mu": g.tb_share_mu, "jurala_share_mu": g.jurala_share_mu}))
    _map("daily_grid_summary", "core_state_grid_daily", {c_: c_ for c_ in g.columns if c_ not in (
        "source_row_no", "quality_flags", "data_source")} | {"data_source": "UNMAPPED (always SYNTHETIC)"})

    con.commit()
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"foreign key violations after core load: {bad[:5]}")
    (HERE / "metadata" / "_lineage.json").write_text(json.dumps(LINEAGE, indent=1), encoding="utf8")
    log("core: foreign keys verified")


# ============================================================================= reconcile
def stage_reconcile(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("ledger_reconcile_log", "ledger_reconciliation"))
    anchors = json.loads(ANCHORS.read_text(encoding="utf8"))
    lines = reconcile.apply(con, anchors, log)
    con.execute("CREATE TABLE ledger_reconcile_log (line_no INTEGER PRIMARY KEY, message TEXT NOT NULL)")
    con.executemany("INSERT INTO ledger_reconcile_log VALUES (?,?)", list(enumerate(lines, 1)))
    results = reconcile.verify(con, anchors)
    con.execute("""CREATE TABLE ledger_reconciliation (check_no INTEGER PRIMARY KEY, label TEXT NOT NULL,
                   expected REAL, actual REAL, diff REAL, tolerance REAL, status TEXT NOT NULL)""")
    con.executemany("INSERT INTO ledger_reconciliation VALUES (?,?,?,?,?,?,?)",
                    [(i, r["label"], r["expected"], r["actual"], r["diff"], r["tol"], r["status"])
                     for i, r in enumerate(results, 1)])
    con.commit()
    failed = [r for r in results if r["status"] == "FAIL"]
    log(f"reconcile: {len(results) - len(failed)} PASS, {len(failed)} FAIL of {len(results)} checks")
    if failed:
        raise RuntimeError(f"reconciliation failed: {failed[:3]}")


# ============================================================================= mart
def stage_mart(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("mart_",))
    sql = (HERE / "mart.sql").read_text(encoding="utf8").replace("{{DATA_AS_OF}}", DATA_AS_OF)
    for stmt in [s for s in sql.split(";\n") if re.search(r"\bCREATE\b", s)]:
        t0 = time.time()
        con.executescript(stmt + ";")
        m = re.search(r"CREATE TABLE (\w+)", stmt)
        if m:
            n = con.execute(f"SELECT COUNT(*) FROM {m.group(1)}").fetchone()[0]
            log(f"mart: {m.group(1):40s} {n:>8,d} rows ({time.time() - t0:.1f}s)")
    con.commit()


# ============================================================================= semantic
def stage_semantic(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("semantic_",))
    docs = json.loads((METADATA / "semantic_docs.json").read_text(encoding="utf8"))
    registry = json.loads((METADATA / "metric_registry.json").read_text(encoding="utf8"))
    caps = json.loads((METADATA / "capability_manifest.json").read_text(encoding="utf8"))
    con.executescript("""
    CREATE TABLE semantic_table_doc (table_name TEXT PRIMARY KEY, table_group TEXT NOT NULL, grain TEXT NOT NULL,
        doc TEXT NOT NULL, row_count INTEGER, date_from TEXT, date_to TEXT);
    CREATE TABLE semantic_column_doc (table_name TEXT NOT NULL, column_name TEXT NOT NULL, data_type TEXT,
        unit TEXT, additive INTEGER, doc TEXT, PRIMARY KEY (table_name, column_name));
    CREATE TABLE semantic_metric (metric_key TEXT PRIMARY KEY, label TEXT NOT NULL, unit TEXT, definition TEXT NOT NULL);
    CREATE TABLE semantic_capability (topic TEXT PRIMARY KEY, status TEXT NOT NULL, mart_tables TEXT, core_tables TEXT,
        row_count INTEGER, note TEXT NOT NULL);
    CREATE TABLE semantic_data_period (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    units = docs["unit_suffixes"]
    mart_tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mart_%' ORDER BY name")]
    missing = sorted(set(mart_tables) - set(docs["tables"]))
    if missing:
        raise RuntimeError(f"mart tables without documentation in semantic_docs.json: {missing}")
    for t in mart_tables:
        td = docs["tables"][t]
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = [(r[1], r[2]) for r in con.execute(f"PRAGMA table_info({t})")]
        dfrom = dto = None
        datecol = td.get("date_column")
        if datecol:
            dfrom, dto = con.execute(f"SELECT MIN({datecol}), MAX({datecol}) FROM {t}").fetchone()
        con.execute("INSERT INTO semantic_table_doc VALUES (?,?,?,?,?,?,?)",
                    (t, td["group"], td["grain"], td["doc"], n, dfrom, dto))
        coldocs = td.get("columns", {})
        for c, typ in cols:
            unit, additive = None, None
            for suf, (uname, add) in units.items():
                if c.endswith(suf):
                    unit, additive = uname, add
                    break
            doc = coldocs.get(c) or docs["common_columns"].get(c)
            if doc is None:
                raise RuntimeError(f"undocumented column {t}.{c}")
            if not typ:
                row = con.execute(f"SELECT typeof({c}) FROM {t} WHERE {c} IS NOT NULL LIMIT 1").fetchone()
                typ = {"integer": "INTEGER", "real": "REAL", "text": "TEXT"}.get(row[0] if row else "", "TEXT")
            typ = {"INT": "INTEGER", "NUM": "REAL"}.get(typ, typ)
            con.execute("INSERT INTO semantic_column_doc VALUES (?,?,?,?,?,?)",
                        (t, c, typ or "TEXT", unit, additive, doc))
    for m in registry["metrics"]:
        con.execute("INSERT INTO semantic_metric VALUES (?,?,?,?)", (m["key"], m["label"], m.get("unit"), json.dumps(m)))
    for cap in caps["topics"]:
        n = None
        if cap.get("core_tables"):
            n = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in cap["core_tables"])
            expect_empty = cap["status"] == "not_loaded"
            if expect_empty and n:
                raise RuntimeError(f"capability {cap['topic']} marked not_loaded but has {n} rows")
            if cap["status"] == "loaded" and not n:
                raise RuntimeError(f"capability {cap['topic']} marked loaded but is empty")
        con.execute("INSERT INTO semantic_capability VALUES (?,?,?,?,?,?)", (
            cap["topic"], cap["status"], ",".join(cap.get("mart_tables", [])) or None,
            ",".join(cap.get("core_tables", [])) or None, n, cap["note"]))
    dfrom, dto = con.execute("SELECT MIN(gen_date), MAX(gen_date) FROM core_unit_daily_generation").fetchone()
    con.executemany("INSERT INTO semantic_data_period VALUES (?,?)", [
        ("data_from", dfrom), ("data_to", dto), ("data_as_of", DATA_AS_OF),
        ("latest_full_fy", "2024-25"), ("partial_fy", "2025-26 (1-30 Apr 2025 only)"),
        ("data_nature", "Synthetic 10-year dataset built on real anchor values from the K-eRP document.")])
    con.commit()
    log(f"semantic: {len(mart_tables)} mart tables documented, {len(registry['metrics'])} metrics, "
        f"{len(caps['topics'])} capability topics")


# ============================================================================= ledger
def stage_ledger(con: sqlite3.Connection) -> None:
    drop_prefixed(con, ("ledger_file", "ledger_column", "ledger_issue", "ledger_build"))
    con.executescript("""
    CREATE TABLE ledger_file AS SELECT file_name, file_role, loaded, size_bytes, sha256, note FROM src_file;
    CREATE TABLE ledger_column (raw_table TEXT NOT NULL, column_name TEXT NOT NULL, stg_type TEXT, landed_in TEXT NOT NULL,
        PRIMARY KEY (raw_table, column_name));
    CREATE TABLE ledger_issue (issue_id TEXT PRIMARY KEY, severity TEXT NOT NULL, status TEXT NOT NULL,
        title TEXT NOT NULL, detail TEXT NOT NULL, affects TEXT);
    CREATE TABLE IF NOT EXISTS ledger_build (run_at TEXT, stage TEXT, seconds REAL);
    """)
    lineage_path = METADATA / "_lineage.json"
    lineage = json.loads(lineage_path.read_text(encoding="utf8")) if lineage_path.exists() else {}
    types = {(r[0], r[1]): r[2] for r in con.execute("SELECT stg_table, column_name, inferred_type FROM stg__column_type")}
    for name in ROW_SHEETS:
        raw = f"raw_{snake(name)}"
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({raw})") if r[1] != "source_row_no"]
        for col in cols:
            if name in LOADED_SHEETS:
                landed = lineage.get(name, {}).get(col, "UNMAPPED (no core column)")
            else:
                landed = "NOT LOADED (provenance / roll-up sheet; kept in raw_ and stg_ only)"
            con.execute("INSERT INTO ledger_column VALUES (?,?,?,?)",
                        (raw, col, types.get((f"stg_{snake(name)}", col)), landed))
    issues = json.loads((METADATA / "known_issues.json").read_text(encoding="utf8"))
    for i in issues["issues"]:
        con.execute("INSERT INTO ledger_issue VALUES (?,?,?,?,?,?)",
                    (i["id"], i["severity"], i["status"], i["title"], i["detail"], ",".join(i.get("affects", []))))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM ledger_column").fetchone()[0]
    unm = con.execute("SELECT COUNT(*) FROM ledger_column WHERE landed_in LIKE 'UNMAPPED%'").fetchone()[0]
    log(f"ledger: {n} raw columns tracked ({unm} unmapped with reason), {len(issues['issues'])} known issues")


# ============================================================================= checks
def stage_checks(con: sqlite3.Connection) -> None:
    """Cross-check the mart against the workbook's own roll-up sheets and invariants."""
    con.execute("DROP TABLE IF EXISTS ledger_build_check")
    con.execute("CREATE TABLE ledger_build_check (check_name TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT)")
    out = []

    def check(name, ok, detail=""):
        out.append((name, "PASS" if ok else "FAIL", detail))

    # 1. station x FY gross vs the workbook's annual_station_summary (the only difference allowed is FY 2025-26
    #    for RTPS/SGS, where the reconcile stage wrote the document's 06-Apr-2025 figures)
    rows = con.execute("""
        SELECT a.fy, a.project_code, a.gross_gen_mu, ROUND(f.gross_gen_mu, 3)
        FROM stg_annual_station_summary a
        LEFT JOIN mart_fact_station_generation_fy f ON f.fy = a.fy AND f.station_key = a.project_code""").fetchall()
    bad = [r for r in rows if r[3] is None or abs(r[2] - r[3]) > 0.0015]
    bad = [r for r in bad if not (r[0] == "2025-26" and r[1] in ("RTPS", "SGS"))]
    check("mart FY station gross = workbook annual_station_summary", not bad, f"{len(rows)} station-years, mismatches: {bad[:3]}")

    rows = con.execute("""
        SELECT m.ym, m.project_code, m.gross_gen_mu, ROUND(f.gross_gen_mu, 3)
        FROM stg_monthly_station_summary m
        LEFT JOIN mart_fact_station_generation_monthly f ON f.year_month = m.ym AND f.station_key = m.project_code""").fetchall()
    bad = [r for r in rows if r[3] is None or abs(r[2] - r[3]) > 0.0015]
    bad = [r for r in bad if not (r[0] == "2025-04" and r[1] in ("RTPS", "SGS"))]
    check("mart month station gross = workbook monthly_station_summary", not bad, f"{len(rows)} station-months, mismatches: {bad[:3]}")

    n_daily = con.execute("SELECT COUNT(*) FROM mart_fact_unit_generation_daily").fetchone()[0]
    check("daily fact rows = workbook daily rows", n_daily == con.execute(
        "SELECT COUNT(*) FROM raw_daily_unit_generation").fetchone()[0], f"{n_daily:,}")
    s1 = con.execute("SELECT ROUND(SUM(gross_gen_mu), 3) FROM mart_fact_unit_generation_daily").fetchone()[0]
    s2 = con.execute("SELECT ROUND(SUM(gross_gen_mu), 3) FROM mart_fact_station_generation_fy").fetchone()[0]
    check("unit-daily total = station-FY total", abs(s1 - s2) < 0.01, f"{s1} vs {s2}")
    hrs = con.execute("SELECT COUNT(*) FROM core_unit_daily_generation WHERE ABS(running_hrs + forced_outage_hrs + "
                      "planned_outage_hrs + rsd_hrs - 24) > 0.05").fetchone()[0]
    check("hours add to 24 on every unit-day", hrs == 0, str(hrs))
    kp = con.execute("SELECT ROUND(SUM(gross_gen_mu), 1) FROM mart_fact_station_generation_fy WHERE fy = '2024-25'").fetchone()[0]
    check("KPCL FY 2024-25 gross = workbook validation figure 37602.2 MU", abs(kp - 37602.2) < 0.05, str(kp))
    plf = con.execute("SELECT plf_pct FROM mart_fact_station_generation_monthly WHERE station_key='RTPS' "
                      "AND month_label='MAR-2025'").fetchone()[0]
    check("RTPS PLF MAR-2025 = RTPS MPR 64.95%", abs(plf - 64.95) < 0.006, str(plf))
    status = con.execute("SELECT unit_key FROM mart_fact_unit_current_status WHERE status <> 'RUNNING' ORDER BY 1").fetchall()
    check("units out at data end = RTPS-U1, RTPS-U4, SIRWAR-U1 (+ April-2025 outages)",
          {"RTPS-U1", "RTPS-U4", "SIRWAR-U1"} <= {r[0] for r in status}, str([r[0] for r in status]))
    recon = con.execute("SELECT COUNT(*), SUM(status='PASS') FROM ledger_reconciliation").fetchone()
    check("all reconciliation anchors pass", recon[0] == recon[1], f"{recon[1]}/{recon[0]}")
    undocumented = con.execute("""SELECT COUNT(*) FROM semantic_column_doc WHERE doc IS NULL OR doc = ''""").fetchone()[0]
    check("every mart column documented", undocumented == 0, str(undocumented))

    con.executemany("INSERT INTO ledger_build_check VALUES (?,?,?)", out)
    con.commit()
    for name, status, detail in out:
        log(f"check: {status}  {name}  {detail}")
    if any(s == "FAIL" for _, s, _ in out):
        raise RuntimeError("build checks failed")


# ============================================================================= main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--from", dest="start", default="sources", choices=STAGES)
    ap.add_argument("--to", dest="end", default="checks", choices=STAGES)
    ap.add_argument("--only", choices=STAGES)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    stages = [args.only] if args.only else STAGES[STAGES.index(args.start):STAGES.index(args.end) + 1]
    fns = {"sources": stage_sources, "raw": stage_raw, "staging": stage_staging, "core": stage_core,
           "reconcile": stage_reconcile, "mart": stage_mart, "semantic": stage_semantic, "ledger": stage_ledger,
           "checks": stage_checks}
    run_at = dt.datetime.now().isoformat(timespec="seconds")
    timings = []
    for st in stages:
        t0 = time.time()
        log(f"=== stage {st}")
        fns[st](con)
        timings.append((run_at, st, round(time.time() - t0, 1)))
    con.execute("CREATE TABLE IF NOT EXISTS ledger_build (run_at TEXT, stage TEXT, seconds REAL)")
    con.executemany("INSERT INTO ledger_build VALUES (?,?,?)", timings)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("ANALYZE")
    con.close()
    log(f"done: {args.db}")


if __name__ == "__main__":
    main()
