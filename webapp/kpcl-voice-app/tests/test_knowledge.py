"""The general knowledge base (knowledge_base.yaml + app.knowledge) and the vague-period overview."""
from __future__ import annotations

import pytest

from app import guardrails, knowledge, pipeline
from conftest import run


def test_station_profiles_match_the_warehouse(db):
    """Capacity and unit counts in the knowledge base must agree with every data answer."""
    res = run(db.run("SELECT station_key, installed_capacity_mw, unit_count FROM mart_dim_station", max_rows=100,
                     timeout_seconds=10))
    wh = {r[0]: (r[1], r[2]) for r in res.rows}
    kb = {s["key"]: (s["capacity_mw"], s["units"]) for s in knowledge._kb()["stations"]}
    assert set(kb) == set(wh)
    for key, (mw, units) in wh.items():
        assert kb[key][0] == pytest.approx(mw), key
        assert kb[key][1] == units, key


@pytest.mark.parametrize("q, expected", [
    ("what is kpcl", "Government of Karnataka's own power generation company"),
    ("When was KPCL founded?", "20 July 1970"),
    ("who owns kpcl", "wholly owned by the Government of Karnataka"),
    ("why does kpcl exist", "separate body to build and run power stations"),
    ("where is kpcl's head office", "Race Course Road"),
    ("who regulates kpcl", "Electricity Regulatory Commission"),
    ("history of kpcl", "1970: KPCL incorporated"),
    ("what is KERC", "constituted in November 1999"),
    ("what is the role of kerc", "constituted in November 1999"),
    ("what is CEA", "Central Electricity Authority"),
    ("what is the electricity act", "Section 62"),
    ("what is the sharavathi pumped storage project", "2,000 MW"),
    ("what is rpcl", "joint venture of KPCL and Bharat Heavy Electricals"),
    ("tell me about RTPS", "Karnataka's first coal-fired"),
    ("tell me about sharavathy generating station", "ten 103.5 MW units"),
    ("what is a supercritical unit", "critical pressure"),
])
def test_general_questions_are_answered_from_the_knowledge_base(db, q, expected):
    r = run(pipeline.answer_question(q, db, 200, 20))
    assert r.route == "knowledge", (q, r.route)
    assert expected in r.answer
    assert not guardrails._PROVENANCE_RE.search(r.answer), r.answer


@pytest.mark.parametrize("q", [
    "RTPS PLF in March 2024", "how many stations does kpcl have", "what is the storage at linganamakki in march 2024",
    "which station generated the most in FY 2023-24", "kpcl generation trend", "RTPS-U3 outages",
])
def test_data_questions_never_come_from_the_knowledge_base(q):
    assert knowledge.primary(q) is None
    assert knowledge.fallback(q) is None


def test_a_capability_note_still_wins_over_a_loose_match(db):
    r = run(pipeline.answer_question("what is kpcl's billing", db, 200, 20))
    assert r.route == "capability"


def test_a_period_alone_is_in_scope_but_other_topics_are_not():
    assert guardrails.screen_question("what was in march 2024") is None
    assert guardrails.screen_question("what happened in 2023-24") is None
    assert guardrails.screen_question("who is the chief minister of karnataka") == "off_topic"
    assert guardrails.screen_question("tell me a joke") == "off_topic"


@pytest.mark.parametrize("q", ["what was in march 2024", "what happened in March 2024?", "March 2024", "how was march 2024"])
def test_a_month_on_its_own_gets_the_month_overview(db, q):
    r = run(pipeline.answer_question(q, db, 200, 20))
    assert r.route == "report:period_overview", (q, r.route)
    assert r.answer.startswith("**March 2024 at KPCL: 3,872.231 MU generated")
    assert "CEA target" in r.answer and "Thermal PLF" in r.answer and "Karnataka's consumption" in r.answer
    # the station rows add up to the headline
    assert sum(row[2] or 0 for row in r.rows) == pytest.approx(3872.231, abs=0.01)


def test_month_overview_for_one_station_and_fy_questions(db):
    r = run(pipeline.answer_question("how was march 2024 for rtps", db, 200, 20))
    assert r.route == "report:period_overview" and r.answer.startswith("**March 2024 at RTPS: 956.739 MU")
    r = run(pipeline.answer_question("what happened in FY 2023-24", db, 200, 20))
    assert r.route == "report:fy_summary"


@pytest.mark.parametrize("q", ["RTPS PLF in March 2024", "what was the decline in march 2024",
                               "which stations missed their CEA target in March 2024"])
def test_a_specific_question_about_a_month_is_not_turned_into_the_overview(db, q):
    r = run(pipeline.answer_question(q, db, 200, 20))
    assert r.route != "report:period_overview"


# ---------------------------------------------------------------------------------------------- found in the live check
def test_a_level_over_a_month_is_the_closing_position_with_the_range(db):
    from app import metric_lookup
    from app.semantic_layer import extract_entities
    q = "what is the storage at linganamakki in march 2024"
    r = run(metric_lookup.answer(extract_entities(q), db, 20, q))
    assert r.answer.startswith("**The storage of Linganamakki reservoir at the end of March 2024 (31-Mar-2024) was 40.53%.**")
    assert "ranged from 40.53% to 52.55%, averaging 46.50% over 31 days" in r.answer
    q = "linganamakki water level in FY 2023-24"
    r = run(metric_lookup.answer(extract_entities(q), db, 20, q))
    assert "at the end of FY 2023-24 (31-Mar-2024)" in r.answer and "feet" in r.answer


def test_a_value_slot_written_where_the_period_belongs_is_repaired():
    from app.answer import fill_template
    e = {"periods": [{"kind": "month", "value": "MAR-2024", "label": "March 2024"}]}
    assert fill_template("The storage at Linganamakki in {storage_pct} was {storage_pct}.", ["storage_pct"], [(46.5,)], e) \
        == "The storage at Linganamakki in March 2024 was 46.50%."
    assert "46.50%" in fill_template("Storage was {storage_pct}, i.e. {storage_pct}.", ["storage_pct"], [(46.5,)], e)
    assert "{" not in fill_template("Storage was {storage_pct}, i.e. {storage_pct}.", ["storage_pct"], [(46.5,)], e)


def test_a_negative_shortfall_is_narrated_by_its_size():
    from app.answer import explain_rows
    cols = ["station_name", "actual_mu", "cea_target_mu", "shortfall_mu"]
    rows = [("Itnal Solar PV Plant", 0.3, 0.31, -0.01), ("Bellary Thermal Power Station", 800.0, 849.762, -49.762),
            ("Raichur Thermal Power Station", 956.739, 1001.082, -44.343)]
    assert explain_rows(cols, rows, "shortfall_mu").startswith(
        "Bellary Thermal Power Station has the highest shortfall (MU), 49.762 MU")


@pytest.mark.parametrize("q", ["who is the MD of kpcl", "who runs kpcl", "who is the chairman of kpcl"])
def test_office_holders_are_not_guessed(q):
    a = knowledge.primary(q)
    assert "does not name KPCL's current office holders" in a and "official website" in a


def test_no_answer_from_the_yaml_ever_shows_where_it_came_from():
    """Guardrail: no sources, site names, web addresses or 'knowledge base' notes in any general-knowledge answer."""
    kb = knowledge._kb()
    answers = [knowledge._kpcl_answer(q) for q in ("what is kpcl", "who owns kpcl", "when was kpcl founded", "why does kpcl exist",
               "where is the head office", "who regulates kpcl", "history of kpcl", "what is the installed capacity",
               "who is the md", "what is kpcl's cin", "what data do you have")]
    answers += [knowledge._entry_answer(e) for g in knowledge.ENTRY_GROUPS for e in kb[g]]
    answers += [knowledge._station_answer(s) for s in kb["stations"]]
    answers += [knowledge._glossary_answer(t) for t in kb["glossary"]]
    for a in answers:
        assert a and not guardrails._PROVENANCE_RE.search(a), a
        for bad in ("sources:", "http", "wikipedia", "knowledge base", "general information", ".com", ".org"):
            assert bad.lower() not in a.lower(), (bad, a)


# ---------------------------------------------------------------------------------------------- KPCL's organisation,
# rules and public information (from its official website, researched 30 September 2026)
@pytest.mark.parametrize("q, title, expected", [
    ("What is KPCL's organisation structure?", "How KPCL is organised", "Chairman to the Board of Directors"),
    ("Explain the hierarchy of KPCL", "How KPCL is organised", "Technical Director"),
    ("Who reports to the Technical Director?", "How KPCL is organised", "Executive Directors of the three thermal stations"),
    ("What does the board of directors do?", "Board of Directors", "Chief Minister is appointed Chairman"),
    ("What are the board committees?", "Committees of KPCL's Board", "Audit Committee"),
    ("Who chairs the CSR committee?", "Committees of KPCL's Board", "Additional Chief Secretary of the Energy Department"),
    ("How many full-time directors does KPCL have?", "full-time directors", "four full-time directors"),
    ("What are the management levels in KPCL?", "Management levels and cadres", "Junior management"),
    ("What are the cadres of employees in KPCL?", "Management levels and cadres", "workmen cadre"),
    ("How are decisions made in KPCL?", "How decisions are made", "delegation of powers"),
    ("What are KPCL's office timings?", "offices and working hours", "9.30 am to 5.30 pm"),
    ("Where are KPCL's offices?", "offices and working hours", "Green Building"),
    ("What is KPCL's mission?", "Mission", "harmony with the environment"),
    ("Who heads vigilance in KPCL?", "Vigilance in KPCL", "Chief Vigilance Officer"),
    ("Which department controls KPCL?", "Energy Department", "Minister for Energy"),
    ("What is KPCL's CSR policy?", "Corporate Social Responsibility", "at least 2% of the average net profit"),
    ("Summary of KPCL's CSR work", "Corporate Social Responsibility", "Schedule VII"),
    ("What is the whistle blower policy?", "Whistle Blower Policy", "Protected Disclosure"),
    ("How do I report fraud in KPCL?", "Whistle Blower Policy", "Director (HR)"),
    ("What are KPCL's service rules?", "service rules and manuals", "HRD Manual"),
    ("How does KPCL recruit engineers?", "How KPCL recruits", "Kannada language test"),
    ("How do I file an RTI with KPCL?", "under the RTI Act", "Public Information Officer"),
    ("How does KPCL buy things? tenders", "tenders and procurement", "blacklisted firms"),
    ("What information does KPCL publish?", "What information KPCL makes public", "annual reports"),
    ("What awards has KPCL won?", "Awards KPCL has received", "IGBC Platinum"),
    ("What consultancy services does KPCL offer?", "consultancy services", "detailed project reports"),
    ("What is KECML?", "Karnataka EMTA Coal Mines", "Coal Mines (Special Provisions) Act, 2015"),
    ("What are KPCL's proposed projects?", "proposed projects", "Godhna Thermal Power Station"),
    ("What is the Varahi pumped storage project?", "Varahi pumped storage", "1,500 MW"),
    ("Who audits KPCL?", "Audit of KPCL", "Comptroller and Auditor General"),
    ("What are the fly ash rules?", "Fly ash rules", "31 December 2021"),
])
def test_organisation_rules_and_public_information(db, q, title, expected):
    r = run(pipeline.answer_question(q, db, 200, 20))
    assert r.route == "knowledge", (q, r.route, r.answer[:120])
    assert title in r.answer.split("\n")[0] and expected in r.answer, (q, r.answer[:300])
    assert not guardrails._PROVENANCE_RE.search(r.answer), r.answer


@pytest.mark.parametrize("q", ["Who is the chairman of KPCL?", "Who is the MD of KPCL?", "What is the role of the managing director?"])
def test_the_organisation_answers_still_never_name_office_holders(q):
    a = knowledge.primary(q)
    assert "does not name KPCL's current office holders" in a and "official website" in a


def test_generation_records_never_come_from_the_organisation_entries():
    """KPCL's own published generation records differ from this proof-of-concept data, so none are in the YAML."""
    kb = knowledge._kb()
    text = " ".join(e["summary"] for g in knowledge.ENTRY_GROUPS for e in kb[g])
    for record in ("29784", "29,784", "18146", "137.022", "97.075", "66.456"):
        assert record not in text


@pytest.mark.parametrize("q", ["What is the salary of employees in KPCL?", "RTPS PLF in March 2024", "KPCL summary",
                               "Coal received by supplier at BTPS in FY 2024-25"])
def test_money_records_and_data_questions_are_not_taken_over(db, q):
    r = run(pipeline.answer_question(q, db, 200, 20))
    assert r.route != "knowledge", (q, r.route)


def test_strip_provenance_keeps_the_rest_of_the_text():
    t = "**KPCL**\n\nKPCL was founded in 1970. See en.wikipedia.org for more.\n\nSources: en.wikipedia.org, zaubacorp.com."
    assert guardrails.strip_provenance(t) == "**KPCL**\n\nKPCL was founded in 1970."
