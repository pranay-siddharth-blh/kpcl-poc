"""Generate gms_semantic_layer.json from the live database: table/column
comments, metric formulas and synonyms, few-shot question->SQL pairs.

This is the artifact an NL2SQL chatbot should actually load into its prompt
or retrieval index -- not the .sql files. It is regenerated from the schema
comments (single source of truth) plus the two static blocks below, so the
chatbot's knowledge can never drift from what the database actually has.

usage: python build_semantic_layer.py "postgresql://postgres@localhost:5432/gms"
"""
import json
import re
import sys

import psycopg2

DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@localhost:5432/gms"

# ---------------------------------------------------------------------------
# Metrics: every ratio the reports use, as a formula over gms_mart columns.
# An NL2SQL model should look here before inventing an AVG(*_pct) query.
# ---------------------------------------------------------------------------
METRICS = [
    {"name": "PLF", "label": "Plant Load Factor %", "formula": "SUM(gross_gen_mu) / SUM(capacity_mu) * 100",
     "tables": ["fact_unit_generation_daily", "fact_station_generation_daily", "fact_station_generation_monthly", "fact_station_generation_fy"],
     "notes": "capacity_mu = capacity_mw * hours_in_period / 1000. Never AVG a per-row percentage."},
    {"name": "PAF", "label": "Plant Availability Factor %", "formula": "SUM(declared_capacity_mu) / SUM(capacity_mu) * 100",
     "tables": ["fact_unit_generation_daily", "fact_station_generation_daily", "fact_station_generation_monthly", "fact_station_generation_fy"]},
    {"name": "PLF_with_NLD", "label": "PLF including deemed generation %",
     "formula": "SUM(gross_gen_mu + deemed_gen_mu) / SUM(capacity_mu) * 100",
     "tables": ["fact_unit_generation_daily", "fact_station_generation_daily"]},
    {"name": "aux_pct", "label": "Auxiliary consumption %", "formula": "SUM(aux_consumption_mu) / SUM(gross_gen_mu) * 100",
     "tables": ["fact_unit_generation_daily"], "notes": "For station-level aux %, add common_aux_mu from fact_station_common_consumption_daily to the numerator."},
    {"name": "specific_coal", "label": "Specific coal consumption, kg/kWh", "formula": "SUM(coal_consumed_mt) / 1000 / SUM(gross_gen_mu)",
     "tables": ["fact_unit_generation_daily"]},
    {"name": "specific_oil", "label": "Specific oil consumption, ml/kWh", "formula": "SUM(hfo_consumed_kl + ldo_consumed_kl) / SUM(gross_gen_mu)",
     "tables": ["fact_unit_generation_daily"]},
    {"name": "weighted_avg_gcv_coal", "label": "Weighted-average coal GCV, kcal/kg",
     "formula": "SUM(gcv_coal_kcal_kg * coal_consumed_mt) / SUM(coal_consumed_mt)", "tables": ["fact_unit_generation_daily"],
     "notes": "Also used for coal receipts: SUM(gcv_received_kcal_kg*quantity_mt)/SUM(quantity_mt) on fact_coal_receipt."},
    {"name": "weighted_avg_heat_rate", "label": "Weighted-average gross heat rate, kcal/kWh",
     "formula": "SUM(gross_heat_rate_kcal_kwh * gross_gen_mu) / SUM(gross_gen_mu)", "tables": ["fact_unit_generation_daily"]},
    {"name": "net_export_station", "label": "Station net export, MU",
     "formula": "SUM(unit gross_gen_mu) - SUM(unit aux_consumption_mu) - station common_aux_mu",
     "tables": ["fact_station_generation_daily"], "notes": "Already computed as net_export_mu in fact_station_generation_daily/monthly/fy."},
    {"name": "coal_stock_below_norm_days", "label": "Days a station's coal cover fell below the normative level",
     "formula": "COUNT(*) WHERE days_of_stock < normative_days", "tables": ["fact_coal_stock_daily"]},
    {"name": "kpcl_share_of_state_demand", "label": "KPCL share of Karnataka state consumption, %",
     "formula": "SUM(fact_station_generation_daily.gross_gen_mu) / SUM(fact_grid_daily.state_consumption_mu) * 100 for matching date_key",
     "tables": ["fact_station_generation_daily", "fact_grid_daily"]},
    {"name": "outage_hours_by_reason", "label": "Outage hours grouped by reason", "formula": "SUM(outage_hrs) GROUP BY reason_code",
     "tables": ["fact_unit_outage"], "notes": "Filter outage_type='FORCED' for forced-outage-only questions."},
    {"name": "backdown_loss_by_reason", "label": "Generation lost to back-down, by reason", "formula": "SUM(loss_mu) GROUP BY reason_code",
     "tables": ["fact_generation_loss_daily"]},
    {"name": "yoy_growth_pct", "label": "Year-on-year growth %", "formula": "(this_fy.gross_gen_mu / last_fy.gross_gen_mu - 1) * 100",
     "tables": ["fact_station_generation_fy"], "notes": "Self-join fact_station_generation_fy on station_key where fy differs by one."},
]

# ---------------------------------------------------------------------------
# Vocabulary: what report/business terms map to which mart object. Feed this
# to the retrieval step so "PAF", "back-down", "JMR", "NLD" resolve correctly.
# ---------------------------------------------------------------------------
SYNONYMS = {
    "project": "station (station_code / station_name in dim_station)",
    "MU": "million kWh, the standard energy unit used throughout",
    "PLF": "Plant Load Factor % -- see metrics.PLF",
    "PAF": "Plant Availability Factor % -- see metrics.PAF",
    "NLD": "deemed generation lost to no-load-demand back-down; column deemed_gen_mu",
    "back-down / backdown": "generation curtailed below capability; fact_generation_loss_daily, reason_class=BACKDOWN in dim_outage_reason",
    "trip": "a FORCED outage event; fact_unit_outage where outage_type='FORCED'",
    "RSD": "Reserve Shut Down -- unit available but not required; rsd_hrs / outage_type='RSD'",
    "BTF / BTL": "Boiler Tube Failure / Leak; fact_unit_outage where is_btf=true, joined to dim_btf_type / dim_btf_location",
    "JMR": "Joint Meter Reading, the monthly signed billing statement; fact_jmr_meter_monthly",
    "MPR": "Monthly Performance Report; fact_station_generation_monthly / fact_unit_generation (aggregate to month)",
    "water year / WY": "Jun-May hydro accounting year; dim_date.water_year",
    "FY": "financial year, Apr-Mar; dim_date.fy, e.g. '2024-25'",
    "major hydro / minor hydro": "dim_station.plant_category_label = 'MAJOR HYDRO' / 'MINOR HYDRO'",
    "RES": "Renewable Energy Source station on the dashboard = waste-to-energy (BWTEP) in this data",
    "CEA target": "generation_target.cea_target_mu, the Central Electricity Authority monthly target",
    "declared capacity / DC": "declared_capacity_mu, the day's declared availability; drives PAF",
    "ESCOM": "electricity distribution company: BESCOM, MESCOM, CESCOM, HESCOM, GESCOM -- dim_counterparty",
    "sale of energy bill": "fact_energy_bill_escom, joined to dim_billing_group",
}

TABLE_PREFIX = {"dim": "dimension", "fact": "fact"}


def fetch_comments(cur):
    cur.execute("""
        select c.relname, c.relkind, obj_description(c.oid) as table_comment
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'gms_mart' and c.relkind in ('r','v') order by c.relname
    """)
    tables = {r[0]: {"kind": "view" if r[1] == "v" else "table", "comment": r[2]} for r in cur.fetchall()}
    cur.execute("""
        select c.relname, a.attname, format_type(a.atttypid, a.atttypmod), col_description(c.oid, a.attnum)
        from pg_class c join pg_namespace n on n.oid = c.relnamespace join pg_attribute a on a.attrelid = c.oid
        where n.nspname = 'gms_mart' and a.attnum > 0 and not a.attisdropped order by c.relname, a.attnum
    """)
    for t, col, typ, comment in cur.fetchall():
        tables[t].setdefault("columns", []).append({"name": col, "type": re.sub(r"numeric\(.*\)", "numeric", typ), "comment": comment})
    return tables


def main():
    con = psycopg2.connect(DSN)
    cur = con.cursor()
    tables = fetch_comments(cur)
    con.close()

    layer = {
        "schema": "gms_mart",
        "description": "Star-schema NL2SQL layer for KPCL Generation Management System (GMS). "
                        "Query gms_mart.* only; it is a thin view layer over the normalized gms schema. "
                        "Every fact table states its grain in its comment -- read that before writing GROUP BY.",
        "units_glossary": {"_mu": "million kWh", "_kwh": "kWh", "_mw": "megawatt", "_hrs": "hours", "_mt": "tonnes",
                            "_kl": "kilolitres", "_rs": "Indian rupees", "_pct": "percent, 0-100 scale",
                            "_mcft": "million cubic feet", "_cum": "cubic metres", "_kcal_kg": "kcal per kg (coal GCV)",
                            "_kcal_l": "kcal per litre (oil GCV)", "_kcal_kwh": "kcal per kWh (heat rate)"},
        "tables": {name: info for name, info in sorted(tables.items())},
        "metrics": METRICS,
        "synonyms": SYNONYMS,
        "join_rules": [
            "Every fact table has a date_key -> join gms_mart.dim_date for fy / month_label / season / water_year.",
            "Every fact table has a unit_key or station_key (never both required) -> join dim_unit or dim_station for names/category.",
            "Do not join gms_mart views back to gms.* tables directly; gms_mart already carries every attribute you need.",
            "fact_unit_outage.reason_code joins dim_outage_reason WHERE reason_class='OUTAGE'; "
            "fact_generation_loss_daily.reason_code joins WHERE reason_class='BACKDOWN'. Do not cross the two.",
        ],
        "examples": [],  # filled from nl2sql_question_bank.sql at generation time, see below
    }

    bank_path = "nl2sql_question_bank.sql"
    try:
        text = open(bank_path, encoding="utf8").read()
        for q, sql in re.findall(r"-- Q: (.*?)\n(.*?;)\s*(?=\n--|\Z)", text, re.S):
            layer["examples"].append({"question": q.strip(), "sql": sql.strip()})
    except FileNotFoundError:
        pass

    out_path = "gms_semantic_layer.json"
    json.dump(layer, open(out_path, "w", encoding="utf8"), indent=2, default=str)
    print(f"wrote {out_path}: {len(layer['tables'])} tables/views, {len(METRICS)} metrics, "
          f"{len(SYNONYMS)} synonyms, {len(layer['examples'])} example queries")


if __name__ == "__main__":
    main()
