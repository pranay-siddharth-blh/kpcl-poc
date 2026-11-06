"""
Verified query library: reviewed SQL (the numbered blocks of kpcl_semantic_layer/example_queries.sql) plus a
deterministic writer for each, for the report-style questions KPCL asks most. Same idea as the WB POC: even at
temperature 0 a model can return different SQL for the same question; a verified answer is identical every time.

Matching is deliberately conservative (WB POC rules): the normalised question must be very close to a registered
phrasing AND carry exactly the same entity tokens (station codes, years, months, categories), AND ask in the same
direction (highest vs lowest). "BTPS" never matches the RTPS entry; "2023-24" never matches "2024-25".
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from app.answer import explain_rows, fmt_value, pretty_label
from app.config import EXAMPLE_QUERIES_PATH
from app.semantic_layer import load_semantic_layer

Rows = list[dict]


@dataclass(frozen=True)
class Chart:
    type: str
    label_column: str
    value_column: str
    series_label: str = ""


@dataclass(frozen=True)
class VerifiedQuery:
    key: str
    example_no: int
    phrasings: tuple[str, ...]
    render: Callable[[Rows, list[str], list[tuple]], str]
    chart: Chart | None = None

    @property
    def sql(self) -> str:
        return example_sql()[self.example_no]


@lru_cache(maxsize=1)
def example_sql() -> dict[int, str]:
    text = EXAMPLE_QUERIES_PATH.read_text(encoding="utf-8")
    out = {}
    for block in re.split(r"\n(?=-- \d+\. )", text):
        m = re.match(r"-- (\d+)\. (.*)\n", block)
        if m:
            out[int(m.group(1))] = block[m.end():].strip().rstrip(";").strip()
    return out


@lru_cache(maxsize=1)
def example_questions() -> dict[int, str]:
    text = EXAMPLE_QUERIES_PATH.read_text(encoding="utf-8")
    return {int(n): q.strip() for n, q in re.findall(r"^-- (\d+)\. (.*)$", text, re.M)}


REGISTRY: list[VerifiedQuery] = []


def verified(key: str, example_no: int, extra_phrasings=(), chart: Chart | None = None):
    def deco(fn):
        REGISTRY.append(VerifiedQuery(key, example_no, (example_questions()[example_no], *extra_phrasings), fn, chart))
        return fn
    return deco


def _v(col, v):
    return fmt_value(col, v)


def _body(columns, raw_rows, preferred=None):
    return explain_rows(columns, raw_rows, preferred) if len(raw_rows) > 1 else ""


def _answer(headline: str, body: str = "", note: str = "") -> str:
    return "\n\n".join(p for p in (f"**{headline}**", body, note) if p)


# ---------------------------------------------------------------------------------------------- HO dashboard
@verified("v01_category_fy2425", 1, ["KPCL generation by plant category in FY 2024-25",
                                     "Category-wise gross generation and net export for FY 2024-25"],
          Chart("bar", "plant_category", "gross_gen_mu", "Gross generation (MU)"))
def _v01(r, c, raw):
    total = sum(x["gross_gen_mu"] for x in r)
    net = sum(x["net_export_mu"] for x in r)
    return _answer(f"KPCL generated {_v('x_mu', total)} gross and exported {_v('x_mu', net)} net in FY 2024-25.", _body(c, raw),
                   "Major/minor hydro follows the dataset's split (14 major stations), not the HO dashboard's (3).")


@verified("v02_below_cea_mar25", 2, ["Stations that missed their CEA target in March 2025",
                                     "Which stations fell short of the CEA target in March 2025?"],
          Chart("bar", "station", "shortfall_mu", "Shortfall against CEA target (MU)"))
def _v02(r, c, raw):
    if not r:
        return _answer("Every station met its CEA target in March 2025.")
    worst = r[0]
    return _answer(f"{len(r)} stations generated less than their CEA target in March 2025; the largest shortfall was "
                   f"{worst['station']} at {_v('x_mu', abs(worst['shortfall_mu']))} below target.",
                   _body(c, raw, "shortfall_mu"))


@verified("v03_fy_vs_last_fy", 3, ["Compare each station's generation in FY 2024-25 with FY 2023-24",
                                   "Station-wise generation this FY vs last FY"],
          Chart("bar", "station", "yoy_growth_pct", "Change vs FY 2023-24 (%)"))
def _v03(r, c, raw):
    cur = sum(x["fy_2024_25_mu"] or 0 for x in r)
    prev = sum(x["fy_2023_24_mu"] or 0 for x in r)
    up = [x["station"] for x in r if (x["yoy_growth_pct"] or 0) > 0]
    return _answer(f"KPCL generated {_v('x_mu', cur)} in FY 2024-25 against {_v('x_mu', prev)} in FY 2023-24 "
                   f"({(cur / prev - 1) * 100:+.2f}%); {len(up)} of {len(r)} stations grew.", _body(c, raw, "fy_2024_25_mu"))


@verified("v04_sgs_wy", 4, ["Compare SGS generation in water years 2023-24 and 2024-25"])
def _v04(r, c, raw):
    d = {x["water_year"]: x["gross_gen_mu"] for x in r}
    a, b = d.get("2023-24"), d.get("2024-25")
    return _answer(f"SGS generated {_v('x_mu', b)} in water year 2024-25 against {_v('x_mu', a)} in 2023-24 "
                   f"({(b / a - 1) * 100:+.2f}%).", "",
                   "Water year 2024-25 runs June 2024 to May 2025; the data ends on 30-Apr-2025, so May 2025 is missing.")


# ---------------------------------------------------------------------------------------------- station daily report
@verified("v05_rtps_units_060425", 5, ["RTPS unit-wise generation on 06-Apr-2025", "RTPS daily report for 6 April 2025"],
          Chart("bar", "unit", "gross_gen_mu", "Gross generation (MU)"))
def _v05(r, c, raw):
    total = sum(x["gross_gen_mu"] for x in r)
    off = [x["unit"] for x in r if not x["gross_gen_mu"]]
    return _answer(f"RTPS generated {_v('x_mu', total)} on 06-Apr-2025 from {len(r) - len(off)} of its {len(r)} units"
                   + (f"; {' and '.join(off)} were out all day." if off else "."), _body(c, raw, "gross_gen_mu"),
                   "These unit figures are the K-eRP RTPS Daily Generation Report values.")


@verified("v06_rtps_best_day", 6, ["What is the highest single-day generation of RTPS ever?",
                                   "RTPS record daily generation"])
def _v06(r, c, raw):
    ever = next(x for x in r if x["scope"] == "ever")
    fy = next(x for x in r if x["scope"] != "ever")
    return _answer(f"RTPS's highest single-day generation is {_v('x_mu', ever['gross_gen_mu'])} on {pretty_label(ever['on_date'])}; "
                   f"in FY 2024-25 its best day was {pretty_label(fy['on_date'])} with {_v('x_mu', fy['gross_gen_mu'])}.",
                   note="The K-eRP report prints 41.892 MU on 26-Oct-2016 as the all-time record; the dataset's history is synthetic.")


# ---------------------------------------------------------------------------------------------- MPR
@verified("v07_rtps_mpr_mar25", 7, ["RTPS monthly performance report for March 2025", "RTPS MPR March 2025 unit-wise"],
          Chart("bar", "unit", "plf_pct", "PLF (%)"))
def _v07(r, c, raw):
    run = [x for x in r if x["gross_gen_mu"]]
    best = max(run, key=lambda x: x["plf_pct"])
    total = sum(x["gross_gen_mu"] for x in r)
    return _answer(f"RTPS units generated {_v('x_mu', total)} in March 2025; {best['unit']} had the highest PLF at "
                   f"{_v('plf_pct', best['plf_pct'])}.", _body(c, raw, "plf_pct"),
                   "Unit gross generation matches the K-eRP RTPS MPR for March 2025; the other parameters are from the dataset.")


@verified("v08_rtps_fy_and_mar", 8, ["RTPS gross generation for FY 2024-25 and March 2025"])
def _v08(r, c, raw):
    d = {x["period"]: x["gross_gen_mu"] for x in r}
    return _answer(f"RTPS generated {_v('x_mu', d['FY 2024-25'])} in FY 2024-25, of which {_v('x_mu', d['MAR-2025'])} in March 2025.",
                   note="Both figures match the K-eRP RTPS Monthly Performance Report.")


@verified("v09_btps_heat_rate", 9, ["BTPS heat rate vs design heat rate by unit in FY 2024-25"],
          Chart("bar", "unit", "actual_heat_rate_kcal_kwh", "Actual gross heat rate (kcal/kWh)"))
def _v09(r, c, raw):
    worst = max(r, key=lambda x: x["actual_heat_rate_kcal_kwh"] - x["design_heat_rate_kcal_kwh"])
    return _answer(f"In FY 2024-25 {worst['unit']} ran furthest from its design heat rate: "
                   f"{_v('x_kcal_kwh', worst['actual_heat_rate_kcal_kwh'])} against {_v('x_kcal_kwh', worst['design_heat_rate_kcal_kwh'])}.",
                   _body(c, raw, "actual_heat_rate_kcal_kwh"))


@verified("v10_rtps_cumulative", 10, ["RTPS cumulative generation since commissioning by unit",
                                      "Life-to-date generation of RTPS units"],
          Chart("bar", "unit", "cum_gen_mu", "Cumulative generation (MU)"))
def _v10(r, c, raw):
    total = sum(x["cum_gen_mu"] for x in r)
    return _answer(f"RTPS units have generated {_v('x_mu', total)} since first synchronisation (up to 30-Apr-2025).",
                   _body(c, raw, "cum_gen_mu"), "Life-to-date figures for RTPS units 1-4 have not been checked against the K-eRP report.")


# ---------------------------------------------------------------------------------------------- back-down, outages
@verified("v11_rtps_backdown", 11, ["RTPS back-down by reason in FY 2024-25", "Why did RTPS back down in FY 2024-25?"],
          Chart("bar", "reason", "loss_mu", "Generation lost (MU)"))
def _v11(r, c, raw):
    total = sum(x["loss_mu"] for x in r)
    return _answer(f"RTPS lost {_v('x_mu', total)} to back-down in FY 2024-25; the largest reason was "
                   f"{r[0]['reason'].lower()} ({_v('x_mu', r[0]['loss_mu'])}).", _body(c, raw, "loss_mu"))


@verified("v12_btps_forced_2324", 12, ["BTPS forced outages by reason in FY 2023-24"],
          Chart("bar", "reason", "outages", "Forced outages"))
def _v12(r, c, raw):
    n = sum(x["outages"] for x in r)
    hrs = sum(x["outage_hrs"] for x in r)
    return _answer(f"BTPS units had {n} forced outages in FY 2023-24, lasting {_v('x_hrs', hrs)} in all.", _body(c, raw, "outages"))


@verified("v13_btps_u3_btf_jan24", 13, ["Boiler tube failures of BTPS unit 3 in January 2024"])
def _v13(r, c, raw):
    if not r:
        return _answer("BTPS-U3 had no boiler tube failures in January 2024.")
    lines = [f"•  {pretty_label(x['trip_at'][:10])} to {pretty_label((x['sync_at'] or '')[:10]) or 'still out'}: "
             f"{x['failure_type'].title()} at the {x['location'].lower()}, {_v('x_hrs', x['outage_hrs'])}" for x in r]
    return _answer(f"BTPS-U3 had {len(r)} boiler tube failures in January 2024.", "\n".join(lines),
                   "These two failures are documented in the K-eRP BTF register; the dates are as recorded in the dataset.")


@verified("v14_btf_location", 14, ["Most common boiler tube failure location", "Where do boiler tubes fail most often?"],
          Chart("bar", "location", "failures", "Boiler tube failures"))
def _v14(r, c, raw):
    total = sum(x["failures"] for x in r)
    return _answer(f"Most boiler tube failures are in the {r[0]['location'].lower()}: {r[0]['failures']} of {total}.", _body(c, raw, "failures"))


@verified("v15_units_down_now", 15, ["Which units are currently not running?", "Which units are down right now?",
                                     "Units under outage now", "Which units are currently down?",
                                     "Which units are out of service now?", "Which units are under outage currently?",
                                     "What units are shut down at present?", "List the units that are down now"])
def _v15(r, c, raw):
    if not r:
        return _answer("All units were running at the end of the data (30-Apr-2025).")
    lines = [f"•  {x['unit']}: {x['status'].replace('_', ' ').lower()} since {pretty_label(str(x['status_since'])[:10])} "
             f"({x['days_in_status']:,.0f} days)" for x in r]
    return _answer(f"{len(r)} units were not running at the end of the data (30-Apr-2025).", "\n".join(lines),
                   "'Now' means the last day in the data, 30-Apr-2025.")


# ---------------------------------------------------------------------------------------------- coal
@verified("v16_coal_below_norm_2425", 16, ["Days of coal stock below norm by station in FY 2024-25"],
          Chart("bar", "station", "days_below_norm", "Days below normative stock"))
def _v16(r, c, raw):
    return _answer(f"In FY 2024-25 {r[0]['station']} spent the most days below its normative coal stock: {r[0]['days_below_norm']} of 365.",
                   _body(c, raw, "days_below_norm"))


@verified("v17_coal_suppliers_2425", 17, ["Coal received by supplier in FY 2024-25"],
          Chart("bar", "supplier", "quantity_mt", "Coal received (tonnes)"))
def _v17(r, c, raw):
    total = sum(x["quantity_mt"] for x in r)
    return _answer(f"KPCL's coal stations received {_v('x_mt', total)} of coal in FY 2024-25; the largest supplier was "
                   f"{r[0]['supplier']} with {_v('x_mt', r[0]['quantity_mt'])}.", _body(c, raw, "quantity_mt"))


@verified("v18_rtps_coal_060425", 18, ["RTPS coal position on 06-Apr-2025", "RTPS coal stock on 6 April 2025"])
def _v18(r, c, raw):
    x = r[0]
    return _answer(f"RTPS closed 06-Apr-2025 with {_v('x_mt', x['closing_stock_mt'])} of coal, {x['days_of_stock']} days of stock.",
                   "\n".join([f"•  Opening stock: {_v('x_mt', x['opening_stock_mt'])}", f"•  Received: {_v('x_mt', x['receipt_mt'])}",
                              f"•  Consumed: {_v('x_mt', x['total_consumed_mt'])} ({_v('x_mt', x['indigenous_consumed_mt'])} domestic, "
                              f"{_v('x_mt', x['imported_consumed_mt'])} imported)"]),
                   "On this date RTPS unit coal (document, 20,572 t) and the coal-stock ledger (dataset) differ.")


# ---------------------------------------------------------------------------------------------- reservoirs, grid
@verified("v19_linganamakki_060425", 19, ["Linganamakki dam level on 06-Apr-2025 compared with last year"])
def _v19(r, c, raw):
    d = {x["on_date"]: x for x in r}
    a, b = d["2024-04-06"], d["2025-04-06"]
    return _answer(f"Linganamakki stood at {b['water_level']:,.2f} ft ({b['capacity_pct']}% full, {_v('x_mu', b['energy_potential_mu'])}) "
                   f"on 06-Apr-2025, against {a['water_level']:,.2f} ft ({a['capacity_pct']}% full) a year earlier.",
                   note="The HO dashboard for 06-Apr-2025 prints 1781.4 ft, 37.59% and 1,728.58 MU.")


@verified("v20_supa_fed_stations", 20, ["Which stations are fed by Supa dam and what did they generate in water year 2024-25?"])
def _v20(r, c, raw):
    return _answer(f"Supa dam feeds {' and '.join(x['station'] for x in r)}, which together generated "
                   f"{_v('x_mu', sum(x['gross_gen_mu'] for x in r))} in water year 2024-25.", _body(c, raw, "gross_gen_mu"))


@verified("v21_kpcl_share_mar25", 21, ["KPCL share of state consumption in March 2025",
                                       "What share of Karnataka's consumption did KPCL generate in March 2025?"])
def _v21(r, c, raw):
    return _answer(f"KPCL's generation equalled {r[0]['kpcl_share_pct']}% of Karnataka's electricity consumption in March 2025.")


# ---------------------------------------------------------------------------------------------- trends
@verified("v22_sgs_season_2324", 22, ["SGS generation by season in FY 2023-24"],
          Chart("bar", "season", "gross_gen_mu", "Gross generation (MU)"))
def _v22(r, c, raw):
    return _answer(f"SGS generated most in the {r[0]['season'].lower().replace('_', '-')} season of FY 2023-24: "
                   f"{_v('x_mu', r[0]['gross_gen_mu'])}.", _body(c, raw, "gross_gen_mu"))


@verified("v23_thermal_quarter_plf_2425", 23, ["Quarter-wise PLF of thermal stations in FY 2024-25"])
def _v23(r, c, raw):
    best = max(r, key=lambda x: x["plf_pct"] or 0)
    lines = []
    for st in sorted({x["station"] for x in r}):
        qs = [x for x in r if x["station"] == st]
        lines.append(f"•  {st}: " + ", ".join(f"{x['fy_quarter']} {_v('plf_pct', x['plf_pct'])}" for x in qs))
    return _answer(f"The best thermal quarter in FY 2024-25 was {best['station']} in {best['fy_quarter']} at "
                   f"{_v('plf_pct', best['plf_pct'])} PLF.", "\n".join(lines))


@verified("v24_weekday_weekend_2425", 24, ["Weekday vs weekend average daily generation in FY 2024-25"])
def _v24(r, c, raw):
    d = {x["day_type"]: x["avg_daily_gen_mu"] for x in r}
    return _answer(f"In FY 2024-25 KPCL averaged {_v('x_mu', d['weekday'])} a day on weekdays and "
                   f"{_v('x_mu', d['weekend'])} on weekends.")


@verified("v25_top_plf_2425", 25, ["Top 5 stations by PLF in FY 2024-25", "Which stations had the highest PLF in FY 2024-25?"],
          Chart("bar", "station", "plf_pct", "PLF (%)"))
def _v25(r, c, raw):
    return _answer(f"{r[0]['station']} had the highest PLF in FY 2024-25 at {_v('plf_pct', r[0]['plf_pct'])}.", _body(c, raw, "plf_pct"))


@verified("v26_monthly_trend_2425", 26, ["Monthly KPCL generation trend in FY 2024-25", "Month-wise generation for FY 2024-25"],
          Chart("line", "year_month", "gross_gen_mu", "Gross generation (MU)"))
def _v26(r, c, raw):
    return _answer(f"KPCL generated {_v('x_mu', sum(x['gross_gen_mu'] for x in r))} in FY 2024-25.", _body(c, raw, "gross_gen_mu"))


@verified("v27_fy_trend", 27, ["KPCL generation by financial year with year-on-year growth",
                               "How has KPCL's annual generation changed over the years?"],
          Chart("line", "fy", "gross_gen_mu", "Gross generation (MU)"))
def _v27(r, c, raw):
    last = r[-1]
    return _answer(f"KPCL generated {_v('x_mu', last['gross_gen_mu'])} in FY {last['fy']}, "
                   f"{abs(last['yoy_growth_pct']):.2f}% {'more' if last['yoy_growth_pct'] > 0 else 'less'} than the year before.",
                   _body(c, raw, "gross_gen_mu"), "FY 2025-26 is left out because the data holds only April 2025.")


@verified("v28_thermal_cea_2425", 28, ["CEA target achievement of thermal stations in FY 2024-25"],
          Chart("bar", "station", "cea_target_achievement_pct", "Achievement vs CEA target (%)"))
def _v28(r, c, raw):
    met = [x["station"] for x in r if x["cea_target_achievement_pct"] >= 100]
    return _answer(f"{len(met)} of the {len(r)} thermal stations met their CEA target in FY 2024-25"
                   + (f" ({', '.join(met)})." if met else "."), _body(c, raw, "cea_target_achievement_pct"))


@verified("v29_thermal_sp_coal_2425", 29, ["Specific coal consumption of thermal stations in FY 2024-25"],
          Chart("bar", "station", "sp_coal_kg_kwh", "Specific coal (kg/kWh)"))
def _v29(r, c, raw):
    return _answer(f"{r[0]['station']} burnt the least coal per unit in FY 2024-25: {_v('sp_coal_kg_kwh', r[0]['sp_coal_kg_kwh'])}.",
                   _body(c, raw, "sp_coal_kg_kwh"))


@verified("v30_best_month_2425", 30, ["Which month of FY 2024-25 had the highest KPCL generation?"])
def _v30(r, c, raw):
    return _answer(f"{pretty_label(r[0]['month_label'])} was KPCL's highest month of FY 2024-25, with {_v('x_mu', r[0]['gross_gen_mu'])}.")


@verified("v31_rtps_unit_aux_2425", 31, ["Auxiliary consumption % of RTPS units in FY 2024-25"],
          Chart("bar", "unit", "aux_pct", "Auxiliary consumption (%)"))
def _v31(r, c, raw):
    run = [x for x in r if x["aux_pct"] is not None]
    return _answer(f"RTPS units used between {_v('aux_pct', min(x['aux_pct'] for x in run))} and "
                   f"{_v('aux_pct', max(x['aux_pct'] for x in run))} of their generation as auxiliary power in FY 2024-25.",
                   _body(c, raw, "aux_pct"))


@verified("v32_hydro_wy_trend", 32, ["Total hydro generation by water year"],
          Chart("line", "water_year", "gross_gen_mu", "Hydro generation (MU)"))
def _v32(r, c, raw):
    full = [x for x in r if x["water_year"] not in ("2014-15", "2024-25")]
    best = max(full, key=lambda x: x["gross_gen_mu"])
    return _answer(f"The best complete water year for KPCL hydro was {best['water_year']} with {_v('x_mu', best['gross_gen_mu'])}.",
                   _body(c, raw, "gross_gen_mu"),
                   "Water year 2014-15 holds only April-May 2015 and 2024-25 ends on 30-Apr-2025, so both are partial.")


@verified("v34_btps_vs_ytps_2425", 34, ["Compare BTPS and YTPS generation and PLF in FY 2024-25"])
def _v34(r, c, raw):
    d = {x["station"]: x for x in r}
    return _answer(f"In FY 2024-25 BTPS generated {_v('x_mu', d['BTPS']['gross_gen_mu'])} at {_v('plf_pct', d['BTPS']['plf_pct'])} PLF, "
                   f"and YTPS {_v('x_mu', d['YTPS']['gross_gen_mu'])} at {_v('plf_pct', d['YTPS']['plf_pct'])} PLF.")


# ---------------------------------------------------------------------------------------------- matching
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                 "nine": "9", "ten": "10", "first": "1", "second": "2", "third": "3", "fourth": "4"}
_MONTHS = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
           "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november", "dec": "december"}


@lru_cache(maxsize=1)
def _entity_words() -> frozenset[str]:
    layer = load_semantic_layer()
    ents = layer["entities"]
    generic = {"power", "house", "station", "dam", "plant", "energy", "to", "farm", "cycle", "combined", "waste", "ph",
               "dph", "mw", "hydro", "thermal", "solar", "wind", "gas", "of", "the", "and", "5", "10", "3"}
    words = {w for s in ents["stations"] for w in normalize(s["key"]).split()}
    for aliases in list(ents["station_aliases"].values()) + list(ents["reservoir_aliases"].values()):
        for a in aliases:
            words.update(w for w in normalize(a).split() if w not in generic)
    words |= {"thermal", "hydro", "major", "minor", "solar", "wind", "gas", "mcl", "sccl", "wcl", "imported",
              "monsoon", "summer", "winter", "weekday", "weekend", "forced", "planned"} | set(_MONTHS.values()) | {"may"}
    return frozenset(words)


def normalize(text: str) -> str:
    s = text.lower().replace("&", " and ").replace("%", " percent ")
    s = re.sub(r"\b(\d{1,2})-(apr|mar|jan|feb|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-(\d{4})\b", r"\1 \2 \3", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = []
    for w in s.split():
        w = _NUMBER_WORDS.get(w, w)
        w = _MONTHS.get(w, w)
        w = {"rtps": "rtps", "raichur": "rtps", "bellary": "btps", "ballari": "btps", "sharavathy": "sgs",
             "sharavathi": "sgs", "fy": "", "financial": "", "year": "year", "06": "6"}.get(w, w)
        if w:
            words.append(w)
    return " ".join(words)


def _signature(norm_text: str) -> frozenset[str]:
    return frozenset(w for w in norm_text.split() if any(ch.isdigit() for ch in w) or w in _entity_words())


_HIGH = {"highest", "most", "top", "largest", "biggest", "maximum", "max", "best", "record", "more"}
_LOW = {"lowest", "least", "fewest", "smallest", "minimum", "min", "bottom", "worst", "less"}


def _direction(norm_text: str) -> int:
    w = set(norm_text.split())
    return (1 if w & _HIGH else 0) - (1 if w & _LOW else 0)


@lru_cache(maxsize=1)
def _index() -> list[tuple[VerifiedQuery, str, frozenset[str]]]:
    return [(q, normalize(p), _signature(normalize(p))) for q in REGISTRY for p in q.phrasings]


MATCH_THRESHOLD = 0.86


def _similarity(a: str, b: str) -> float:
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(seq, jac)


def match(question: str) -> VerifiedQuery | None:
    n = normalize(question)
    sig, direction = _signature(n), _direction(n)
    best, best_score = None, 0.0
    for q, p_norm, p_sig in _index():
        if p_sig != sig or direction * _direction(p_norm) < 0:
            continue
        score = 1.0 if p_norm == n else _similarity(n, p_norm)
        if score > best_score:
            best, best_score = q, score
    return best if best_score >= MATCH_THRESHOLD else None
