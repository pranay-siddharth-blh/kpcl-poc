"""
The full pipeline with the LLM mocked (no gateway / VPN needed), in the style of the Cescom POC's
pipeline_mock_test: routing order, the one LLM call, the repair round, guards, and the offline fallback.
Patch target is app.pipeline.generate_query_plan (the name the pipeline imported), not app.llm's.
"""
import pytest

import app.pipeline as pipeline
from app.llm import LLMUnavailable, parse_query_plan
from app.models import ChartSpec, LLMQueryPlan
from conftest import run


class FakeLLM:
    def __init__(self, *plans):
        self.plans = list(plans)
        self.prompts = []

    async def __call__(self, prompt, retry=False):
        self.prompts.append(prompt)
        p = self.plans.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def ask(db, q, monkeypatch, *plans):
    fake = FakeLLM(*plans)
    monkeypatch.setattr(pipeline, "generate_query_plan", fake)
    return run(pipeline.answer_question(q, db, 200, 20)), fake


GOOD = LLMQueryPlan(
    answerable=True,
    sql="SELECT station_key AS station, SUM(gross_gen_mu) AS gross_gen_mu FROM mart_fact_station_generation_fy "
        "WHERE fy = '2024-25' AND plant_category_label = 'THERMAL-COAL' GROUP BY station_key ORDER BY gross_gen_mu DESC",
    answer_template="Thermal stations generated as shown.",
    chart_data=ChartSpec(type="bar", label_column="station", value_column="gross_gen_mu"))


def test_llm_path_answers_with_narration_chart_and_table(db, monkeypatch):
    r, fake = ask(db, "Compare generation at the thermal stations in FY 2024-25", monkeypatch, GOOD)
    assert r.route == "llm" and r.answerable
    assert r.answer.startswith("**RTPS has the highest gross generation")
    assert r.chart["labels"][0] == "RTPS" and r.table["total_rows"] == 3
    assert len(fake.prompts) == 1 and "mart_fact_station_generation_fy" in fake.prompts[0]
    assert "core_" not in fake.prompts[0].split("## Tables you may query")[1].split("## Metric")[0]


def test_single_row_template_filled_from_real_value(db, monkeypatch):
    plan = LLMQueryPlan(answerable=True, sql="SELECT ROUND(SUM(loss_mu), 3) AS loss_mu FROM mart_fact_generation_loss_daily "
                        "WHERE station_key = 'RTPS' AND fy = '2024-25' AND reason_code = 'NO_LOAD_DEMAND'",
                        answer_template="RTPS lost {loss_mu} to no-load-demand back-down in FY 2024-25.")
    r, _ = ask(db, "How much generation did RTPS lose to no load demand in FY 2024-25 because of backdown?", monkeypatch, plan)
    assert r.route == "llm" and "RTPS lost 1,516.787 MU to no-load-demand back-down" in r.answer


def test_unsafe_sql_triggers_one_repair_with_the_error(db, monkeypatch):
    bad = LLMQueryPlan(answerable=True, sql="SELECT AVG(plf_pct) AS p FROM mart_fact_station_generation_monthly "
                       "WHERE plant_category_label = 'THERMAL-COAL' AND fy = '2024-25'", answer_template="PLF was {p}.")
    fixed = LLMQueryPlan(answerable=True, sql="SELECT ROUND(SUM(gross_gen_mu) / SUM(capacity_mu) * 100, 2) AS plf_pct "
                         "FROM mart_fact_station_generation_monthly WHERE plant_category_label = 'THERMAL-COAL' "
                         "AND fy = '2024-25'", answer_template="Thermal PLF over the year was {plf_pct}.")
    r, fake = ask(db, "Across all thermal stations what was the overall plant load factor over FY 2024-25, taking every month?",
                  monkeypatch, bad, fixed)
    assert len(fake.prompts) == 2 and "AVG(plf_pct)" in fake.prompts[1] and "SUM(numerator)" in fake.prompts[1]
    assert "51.18%" in r.answer


def test_write_attempt_never_reaches_db(db, monkeypatch):
    bad = LLMQueryPlan(answerable=True, sql="DELETE FROM mart_dim_station", answer_template="done")
    r, fake = ask(db, "Please tidy up the station outages list for BTPS in FY 2023-24 by reason", monkeypatch, bad, bad)
    assert not r.answerable and r.route == "llm_sql_failed" and "Forbidden" in r.error or "SELECT" in r.error
    assert run(db.run("SELECT COUNT(*) FROM mart_dim_station", 1, 5)).rows[0][0] == 33


def test_core_table_read_blocked(db, monkeypatch):
    sneaky = LLMQueryPlan(answerable=True, sql="SELECT COUNT(*) AS n FROM mart_dim_station s, core_station c",
                          answer_template="{n}")
    r, _ = ask(db, "How many stations with outages in BTPS during FY 2023-24 by reason?", monkeypatch, sneaky, sneaky)
    assert not r.answerable


def test_model_refusal_is_scrubbed(db, monkeypatch):
    no = LLMQueryPlan(answerable=False, clarification_message="The mart_fact tables lack it. Bengaluru is lovely.")
    r, _ = ask(db, "What was the RTPS turbine vibration trend in FY 2024-25?", monkeypatch, no)
    assert not r.answerable and "mart_fact" not in r.answer and "Bengaluru" not in r.answer


def test_offline_fallback(db, monkeypatch):
    r, _ = ask(db, "What was the RTPS turbine vibration trend in FY 2024-25?", monkeypatch, LLMUnavailable("x"))
    assert r.route == "llm_unavailable" and "VPN" in r.answer


@pytest.mark.parametrize("q,route", [
    ("What was RTPS's PLF in March 2025?", "metric:plf"),
    ("Which units are down right now?", "verified:v15_units_down_now"),
    ("What is PAF?", "glossary"),
    ("Show the JMR for BTPS in March 2025", "capability"),
    ("who are you", "identity"),
    ("What is the capital of Karnataka?", "refused:off_topic"),
    ("RTPS generation in March 2014", "out_of_range"),
    ("What was generation of a station called ZZTPS in FY 2024-25?", "unknown_name"),
    ("RTPS-U12 generation in FY 2024-25", "unknown_unit"),
    ("Which units are currently down?", "verified:v15_units_down_now"),
    ("Who is the chief minister of Karnataka?", "refused:off_topic"),
])
def test_deterministic_routes_never_call_the_llm(db, monkeypatch, q, route):
    r, fake = ask(db, q, monkeypatch)  # no plans queued: any LLM call would raise IndexError
    assert r.route == route and not fake.prompts


def test_parse_query_plan_is_defensive():
    p = parse_query_plan('<think>hmm</think>```json\n{"answerable": true, "sql": "SELECT 1 AS x", '
                         '"answer_template": "{x}", "chart_data": {"chart_type": "pie", "label_column": "a"}}\n```')
    assert p.answerable and p.chart_data is None and p.clarification_message == ""
