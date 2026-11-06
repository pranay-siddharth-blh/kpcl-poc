"""The second model pass: every model-written answer is reviewed before it is shown (models mocked, no VPN)."""
import pytest

from app import pipeline
from app.guardrails import REFUSALS
from app.llm import Verdict, parse_verdict
from app.models import LLMQueryPlan
from conftest import run

Q = "Give me the PAF of each BTPS unit for March 2025"
GOOD = LLMQueryPlan(answerable=True, answer_template="", sql=(
    "SELECT unit_key AS unit, ROUND(SUM(declared_capacity_mu) * 100.0 / SUM(capacity_mu), 2) AS paf_pct "
    "FROM mart_fact_unit_generation_monthly WHERE station_key = 'BTPS' AND month_label = 'MAR-2025' GROUP BY unit_key"))
WRONG = LLMQueryPlan(answerable=True, answer_template="", sql=(      # PLF instead of PAF: passes every SQL guard
    "SELECT unit_key AS unit, ROUND(SUM(gross_gen_mu) * 100.0 / SUM(capacity_mu), 2) AS plf_pct "
    "FROM mart_fact_unit_generation_monthly WHERE station_key = 'BTPS' AND month_label = 'MAR-2025' GROUP BY unit_key"))


def setup(monkeypatch, plans, verdicts):
    plans, verdicts, seen = list(plans), list(verdicts), {"plans": 0, "reviews": [], "repair_prompts": []}

    async def gen(prompt, retry=False):
        seen["plans"] += 1
        if "A reviewer rejected your previous answer" in prompt:
            seen["repair_prompts"].append(prompt)
        return plans.pop(0)

    async def review(prompt):
        seen["reviews"].append(prompt)
        v = verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(pipeline, "generate_query_plan", gen)
    monkeypatch.setattr(pipeline, "verify_answer", review)
    monkeypatch.setattr(pipeline.settings, "kpcl_verify_answers", True)
    return seen


def test_approved_answer_is_shown(db, monkeypatch):
    seen = setup(monkeypatch, [GOOD], [Verdict(True)])
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert r.answerable and r.route == "llm" and r.verification == "ok" and "BTPS-U1" in r.answer
    review = seen["reviews"][0]
    assert Q in review and "paf_pct" in review and "BTPS-U1" in review      # question, SQL, rows and answer are all reviewed


def test_rejected_answer_is_repaired_and_rechecked(db, monkeypatch):
    seen = setup(monkeypatch, [WRONG, GOOD], [Verdict(False, "It computes PLF, but the question asks for PAF.", "wrong_measure"), Verdict(True)])
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert r.answerable and r.verification == "fixed" and "paf" in r.sql.lower() and "plf_pct" not in r.sql
    assert len(seen["reviews"]) == 2 and "asks for PAF" in seen["repair_prompts"][0]


def test_answer_rejected_twice_is_never_shown(db, monkeypatch):
    setup(monkeypatch, [WRONG, WRONG], [Verdict(False, "PLF, not PAF.", "wrong_measure"), Verdict(False, "Still PLF.", "wrong_measure")])
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert not r.answerable and r.route == "llm_unverified" and r.answer == REFUSALS["unverified"]
    assert r.table is None and r.chart is None and not r.rows and "%" not in r.answer


def test_reviewer_unreachable_fails_open(db, monkeypatch):
    setup(monkeypatch, [GOOD], [RuntimeError("gateway timeout")])
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert r.answerable and r.verification == "skipped" and "BTPS-U1" in r.answer


def test_deterministic_routes_are_not_reviewed(db, monkeypatch):
    seen = setup(monkeypatch, [], [])
    r = run(pipeline.answer_question("What was RTPS's PLF in March 2025?", db, 200, 20))
    assert r.answerable and "64.95%" in r.answer and not seen["reviews"]


def test_review_can_be_switched_off(db, monkeypatch):
    seen = setup(monkeypatch, [GOOD], [])
    monkeypatch.setattr(pipeline.settings, "kpcl_verify_answers", False)
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert r.answerable and r.verification == "off" and not seen["reviews"]


@pytest.mark.parametrize("raw,ok", [
    ('{"ok": true, "problem": ""}', True), ('```json\n{"ok": false, "problem": "wrong period"}\n```', False),
    ('<think>checking</think>{"ok": "yes"}', True), ('Sure. {"ok": false, "problem": "x"} Done.', False),
])
def test_parse_verdict(raw, ok):
    assert parse_verdict(raw).ok is ok


# ------------------------------------------------------------------ the deterministic veto on false objections
@pytest.mark.parametrize("verdict", [
    Verdict(False, "The SQL must filter on company_code = 'KPCL'.", "wrong_scope"),          # forbidden by the rules
    Verdict(False, "The SQL is for the wrong station.", "wrong_scope"),                       # it filters BTPS
    Verdict(False, "The SQL filters fy which is text like '2024-25'.", "wrong_period"),      # it filters MAR-2025
    Verdict(False, "PAF is computed wrongly.", "wrong_measure"),                              # uses the PAF formula
    Verdict(False, "The answer rounds 57.2 to 57.", "unsupported_claim"),                     # figures come from rows
    Verdict(False, "Please use a nicer tone.", "style"),                                      # not an error category
])
def test_false_objections_are_overruled(db, monkeypatch, verdict):
    seen = setup(monkeypatch, [GOOD], [verdict])
    r = run(pipeline.answer_question(Q, db, 200, 20))
    assert r.answerable and r.verification == "ok" and "BTPS-U1" in r.answer
    assert len(seen["reviews"]) == 1 and r.review_notes and "overruled" in r.review_notes[0]


def test_reviewer_sees_column_docs(db, monkeypatch):
    seen = setup(monkeypatch, [GOOD], [Verdict(True)])
    run(pipeline.answer_question(Q, db, 200, 20))
    assert "declared_capacity_mu" in seen["reviews"][0] and "Columns used by the SQL" in seen["reviews"][0]


def test_veto_rules_directly():
    from app.review import shape_matches, veto
    from app.semantic_layer import extract_entities
    e = extract_entities("How much coal came from Mahanadi Coalfields in FY 2018-19?")
    sql = "SELECT SUM(quantity_mt) AS quantity_mt FROM mart_fact_coal_receipt WHERE supplier_code = 'MCL' AND fy = '2018-19'"
    assert veto(Verdict(False, "wrong supplier", "wrong_scope"), "wrong_scope", "q", e, sql, ["quantity_mt"], [(5921665,)], "")
    # a real scope error is not vetoed
    bad = sql.replace("'MCL'", "'SCCL'")
    assert veto(Verdict(False, "wrong supplier", "wrong_scope"), "wrong_scope", "q", e, bad, ["quantity_mt"], [(1,)], "") is None
    # shape: one figure asked, 31 daily rows returned -> a real aggregation error
    e2 = extract_entities("State electricity consumption in March 2025")
    assert not shape_matches(e2, [(1,)] * 31) and shape_matches(e2, [(8698.7,)])
    e3 = extract_entities("Compare RTPS generation in March 2025 and March 2024")
    assert shape_matches(e3, [(1,), (2,)])


def test_lookup_steps_aside_for_suppliers_and_reasons(db):
    from app import metric_lookup
    from app.semantic_layer import extract_entities
    for q in ["How much imported coal did BTPS receive in FY 2024-25?", "Back-down due to no load demand at YTPS in FY 2023-24"]:
        assert run(metric_lookup.answer(extract_entities(q), db, 10, q)) is None, q
