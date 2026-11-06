"""
The single request pipeline, shared by the SSE endpoint, the JSON endpoint and the evaluation scripts.
Deterministic routes run first and never call the model; only what is left reaches the one LLM call.

  L1  screen            empty / too long / pasted code / injection               -> fixed refusal
  L2a identity          "who are you"                                             -> fixed answer
  L2b verified          reviewed SQL + writer (example_queries.sql library)       -> answer
  L2c glossary          "what is PAF?"                                            -> plain definition
  L2d capability        needs data that isn't loaded (JMR, billing, defects ...)  -> honest note
  L2e knowledge         "what is KPCL?", "what is KERC?", "tell me about RTPS"    -> knowledge_base.yaml (also the
                        fallback when the model declines or is unreachable)
  L1' screen            questions about the system, off-topic                     -> fixed refusal
  L3  router            entities: stations, units, periods, metrics, intents (no LLM)
  L4  metric lookup     one metric x one scope x one period                       -> answer from the registry
  L5  prompt            pruned schema + formulas + examples + entities
  L6  LLM               one call -> {answerable, sql, answer_template, chart}
  L7  SQL guard         read-only shape, mart-only, grain guards, NULLS LAST
  L8  execute           read-only file + authorizer + timeout + row cap; one repair round on failure
  L9  result guards     subject mismatch, empty result
  L10 answer writer     template fill / narration, units, caveats, table, chart (no second LLM call)
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app import answer as writer
from app import capability, guardrails, knowledge, metric_lookup, reports, review, verified_queries
from app.db import ReadOnlyDB, RestrictedQueryError
from app.guardrails import REFUSALS
from app.config import settings
from app.llm import LLMUnavailable, Verdict, generate_query_plan, verify_answer
from app.prompts import build_prompt, coverage_repair_prompt, repair_prompt, verify_repair_prompt
from app.semantic_layer import (data_as_of, definitional_term, extract_entities, is_identity_question,
                                load_semantic_layer, prune_semantic_layer, router_index, station_unit_count,
                                unknown_names)
from app.sql_safety import UnsafeSQLError, validate_readonly_sql

_log = logging.getLogger(__name__)

IDENTITY_ANSWER = (
    "**I'm the KPCL generation data assistant.** I answer questions about KPCL's 33 power stations and 103 units from "
    "April 2015 to April 2025: gross and net generation, PLF, PAF, auxiliary consumption, fuel and heat rate, outages, "
    "trips and boiler tube failures, back-down losses, CEA targets, coal stock and receipts, reservoir levels and the "
    "state grid, by unit, station, category, day, month, quarter, financial year or water year.\n\n"
    "Try: \"What was RTPS's PLF in March 2025?\", \"Which stations missed their CEA target in March 2025?\" or "
    "\"Which units are down right now?\"")


# Records this system does not hold even when the question is about KPCL's organisation: these keep the capability note.
_MONEY_OR_RECORDS = re.compile(r"\b(salar\w*|pay ?slips?|pension (amount|slip)s?|bills?|billing|billed|invoices?|revenue|rupees|"
                               r"dues|outstanding|meter\w*|hourly|defects?)\b", re.I)


@dataclass
class PipelineResult:
    question: str
    answerable: bool = False
    answer: str = ""
    route: str = ""
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    table: dict | None = None
    chart: dict | None = None
    notes: list[str] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    timing_ms: dict = field(default_factory=dict)
    error: str = ""
    verification: str = ""   # model-written answers: ok / fixed / rejected / skipped / off
    review_notes: list[str] = field(default_factory=list)
    total_row: tuple | None = None   # reviewed reports only: the total row their Excel report writes under the table


Progress = Callable[[str], None]


def _noop(_stage: str) -> None:
    return None


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)


def _finish(result: PipelineResult, answer: str, route: str, answerable: bool = True) -> PipelineResult:
    result.answer, result.route, result.answerable = answer, route, answerable
    return result


async def answer_question(question: str, db: ReadOnlyDB, max_rows: int, timeout_seconds: float,
                          progress: Progress | None = None) -> PipelineResult:
    """`progress(stage)` is called as each real stage starts (the SSE endpoint streams them to the page).
    A figure asked for in another unit ("in GWh", "in lakh tonnes", "in TMC") is converted after the answer is
    written, deterministically, whichever route produced it."""
    result = await _answer_question(question, db, max_rows, timeout_seconds, progress)
    if result.answerable and result.answer:
        result.answer = writer.convert_units(question, result.answer)
    return result


async def _answer_question(question: str, db: ReadOnlyDB, max_rows: int, timeout_seconds: float,
                           progress: Progress | None = None) -> PipelineResult:
    progress = progress or _noop
    progress("understanding")
    result = PipelineResult(question=question)
    t0 = time.monotonic()
    timing = result.timing_ms

    screened = guardrails.screen_question(question)
    if screened in ("empty", "too_long", "code", "injection", "harmful", "read_only", "personal_data"):
        timing["router_ms"] = _ms(t0)
        return _finish(result, REFUSALS[screened], f"refused:{screened}", False)

    if is_identity_question(question):
        timing["router_ms"] = _ms(t0)
        return _finish(result, IDENTITY_ANSWER, "identity")

    vq = verified_queries.match(question)
    if vq is not None:
        progress("querying")
        try:
            qr = await db.run(vq.sql, max_rows=max_rows, timeout_seconds=timeout_seconds)
            timing["db_ms"] = _ms(t0)
            dict_rows = [dict(zip(qr.columns, r)) for r in qr.rows]
            result.sql, result.columns, result.rows, result.truncated = vq.sql, qr.columns, qr.rows, qr.truncated
            result.table = writer.result_table(qr.columns, qr.rows)
            if vq.chart:
                result.chart = writer.build_chart(vq.chart.type, vq.chart.label_column, vq.chart.value_column,
                                                  vq.chart.series_label, qr.columns, qr.rows)
            result.entities = {"verified_query": vq.key}
            timing["fill_ms"] = _ms(t0)
            return _finish(result, vq.render(dict_rows, qr.columns, qr.rows), f"verified:{vq.key}")
        except Exception:  # noqa: BLE001 -- a verified query should never fail; fall through to the model
            _log.exception("verified query %s failed", vq.key)
            result = PipelineResult(question=question)
            timing = result.timing_ms

    # "What is CEA?" asks about the Authority, not the data glossary's nearest term ("CEA target").
    if screened != "system" and knowledge.is_definition(question):
        kb = knowledge.primary(question)
        if kb:
            timing["router_ms"] = _ms(t0)
            return _finish(result, kb, "knowledge")

    term = definitional_term(question)
    if term:
        g = load_semantic_layer()["glossary"][term]
        timing["router_ms"] = _ms(t0)
        return _finish(result, f"**{term}**: {g['plain']}", "glossary")

    # General questions about KPCL itself ("What is KPCL?", "When was it founded?", "What is KERC?", "Tell me about
    # RTPS") come from the knowledge base; runs before the off-topic refusal because "founded" / "history of" trip it.
    # A capability note wins, except over a plain "what is X?" that the knowledge base explains.
    kb = knowledge.primary(question) if screened != "system" else None
    if kb and reports.FIGURES_ASK.search(question) and not reports.PROFILE_ASK.search(question) \
            and not knowledge.is_about_organisation(question):
        kb = None   # "KPCL summary", "RTPS overview", "MIS report of SGS": the figures, not the company profile
    note = capability.check(question)
    org_question = bool(kb) and knowledge.is_about_organisation(question) and not _MONEY_OR_RECORDS.search(question)
    if note and not (kb and (knowledge.is_definition(question) or org_question)):
        timing["router_ms"] = _ms(t0)
        return _finish(result, note, "capability", False)
    if kb:
        timing["router_ms"] = _ms(t0)
        return _finish(result, kb, "knowledge")

    if screened is not None:
        timing["router_ms"] = _ms(t0)
        return _finish(result, REFUSALS[screened], f"refused:{screened}", False)

    entities = extract_entities(question)
    result.entities = entities
    unknown = unknown_names(question)
    if unknown:
        timing["router_ms"] = _ms(t0)
        bad_units = [u for u in unknown if re.fullmatch(r".+-U\d+", u) and u.rsplit("-U", 1)[0] in router_index().stations]
        if bad_units and len(bad_units) == len(unknown):
            st = bad_units[0].rsplit("-U", 1)[0]
            n = station_unit_count(st)
            return _finish(result, f"**{st} has no unit {bad_units[0].rsplit('-U', 1)[1]}: it has {n} unit"
                                   f"{'s' if n != 1 else ''}, {st}-U1{f' to {st}-U{n}' if n > 1 else ''}.** "
                                   f"Please ask about one of those units, or about {st} as a whole.",
                           "unknown_unit", False)
        stations = ", ".join(s["key"] for s in load_semantic_layer()["entities"]["stations"])
        return _finish(result, f"**I don't recognise {' or '.join(repr(u) for u in unknown)} as a KPCL station or unit in "
                               f"this data.** The stations are: {stations}. Units are written like RTPS-U8.",
                       "unknown_name", False)
    out_of_range = [p for p in entities.get("periods", []) if p["kind"] == "out_of_range"]
    if out_of_range and len(out_of_range) == len(entities.get("periods", [])):
        timing["router_ms"] = _ms(t0)
        return _finish(result, f"**The data covers 01-Apr-2015 to {writer.pretty_label(data_as_of())} only, so there is no "
                               f"figure for {out_of_range[0]['label']}.**", "out_of_range", False)
    timing["router_ms"] = _ms(t0)

    # Reviewed, parameterised reports for broad client-style questions ("performance of all thermal stations in
    # FY 2023-24", "which stations need attention", "units at risk", "coal situation" ...).
    t1 = time.monotonic()
    progress("querying")
    try:
        rep = await reports.match_and_run(question, entities, db, timeout_seconds)
    except Exception:  # noqa: BLE001 -- a report should never fail; fall through to the next route
        _log.exception("report failed for %r", question[:200])
        rep = None
    if rep is not None:
        timing["db_ms"] = _ms(t1)
        result.sql, result.columns, result.rows = rep.sql, rep.columns, rep.rows
        result.table = writer.result_table(rep.columns, rep.rows)
        result.chart = rep.chart
        result.total_row = rep.total
        return _finish(result, rep.answer, f"report:{rep.key}")

    t1 = time.monotonic()
    try:
        ma = await metric_lookup.answer(entities, db, timeout_seconds, question)
    except Exception:  # noqa: BLE001
        _log.exception("metric lookup failed for %r", question[:200])
        ma = None
    if ma is not None:
        timing["db_ms"] = _ms(t1)
        result.sql, result.columns, result.rows = ma.sql, ma.columns, ma.rows
        return _finish(result, ma.answer, f"metric:{ma.metric}")

    return await _llm_path(result, question, entities, db, max_rows, timeout_seconds, progress)


async def _llm_path(result: PipelineResult, question: str, entities: dict, db: ReadOnlyDB, max_rows: int,
                    timeout_seconds: float, progress: Progress = _noop) -> PipelineResult:
    timing = result.timing_ms
    progress("writing_sql")
    t0 = time.monotonic()
    pruned = prune_semantic_layer(question, entities)
    prompt = build_prompt(question, pruned, entities)
    timing["prompt_ms"] = _ms(t0)
    timing["prompt_tokens_est"] = len(prompt) // 4

    t0 = time.monotonic()
    try:
        try:
            plan = await generate_query_plan(prompt)
        except (ValueError, KeyError) as exc:  # cut off or malformed JSON: retry once, telling the model what went wrong
            _log.warning("unusable plan (%s); retrying once", exc)
            plan = await generate_query_plan(prompt, retry=True)
    except LLMUnavailable:
        timing["llm_ms"] = _ms(t0)
        kb = knowledge.fallback(question)
        return _finish(result, kb, "knowledge_fallback") if kb else _finish(result, REFUSALS["llm_unavailable"], "llm_unavailable", False)
    except Exception as exc:  # noqa: BLE001
        timing["llm_ms"] = _ms(t0)
        _log.exception("LLM call failed for %r", question[:200])
        result.error = f"LLM call failed: {exc}"
        kb = knowledge.fallback(question)
        return _finish(result, kb, "knowledge_fallback") if kb else _finish(result, REFUSALS["llm_unavailable"], "llm_error", False)
    timing["llm_ms"] = _ms(t0)

    if not plan.answerable and guardrails.false_coverage_claim(plan.clarification_message, entities):
        # The model said a period is outside the data when it is not: correct it once.
        _log.warning("false coverage claim for %r: %s", question[:200], plan.clarification_message[:200])
        t0 = time.monotonic()
        try:
            plan = await generate_query_plan(coverage_repair_prompt(prompt, plan.clarification_message))
        except Exception as exc:  # noqa: BLE001
            result.error = f"coverage repair failed: {exc}"
        timing["coverage_repair_ms"] = _ms(t0)
    if not plan.answerable:
        if guardrails.false_coverage_claim(plan.clarification_message, entities):
            return _finish(result, REFUSALS["not_in_data"], "llm_declined:coverage", False)
        kb = knowledge.fallback(question)
        if kb:
            return _finish(result, kb, "knowledge_fallback")
        return _finish(result, guardrails.safe_refusal(plan.clarification_message), "llm_declined", False)

    first = await _execute_plan(result, question, entities, db, max_rows, timeout_seconds, progress, prompt, plan)
    if isinstance(first, PipelineResult):
        return first
    if not settings.kpcl_verify_answers:
        result.verification = "off"
        return _finish(result, first, "llm")

    # Second model pass: an independent reviewer checks the SQL, the result and the answer against the question.
    verdict = await _review(result, question, entities, first, progress)
    if verdict is None or verdict.ok:
        result.verification = "ok" if verdict else "skipped"
        return _finish(result, first, "llm")

    _log.warning("reviewer rejected the answer to %r: %s", question[:200], verdict.problem)
    result.error = f"reviewer: {verdict.problem}"
    t0 = time.monotonic()
    progress("repairing")
    try:
        plan = await generate_query_plan(verify_repair_prompt(prompt, result.sql, verdict.problem))
    except Exception as exc:  # noqa: BLE001
        _log.warning("repair after review failed for %r: %s", question[:200], exc)
        return _unverified(result)
    result.timing_ms["review_repair_llm_ms"] = _ms(t0)
    if not plan.answerable:
        return _unverified(result)
    second = await _execute_plan(result, question, entities, db, max_rows, timeout_seconds, progress, prompt, plan)
    if isinstance(second, PipelineResult):
        return _unverified(result)
    verdict2 = await _review(result, question, entities, second, progress, attempt=2)
    if verdict2 is not None and not verdict2.ok:
        _log.warning("reviewer rejected the repaired answer to %r: %s", question[:200], verdict2.problem)
        result.error = f"reviewer (after repair): {verdict2.problem}"
        return _unverified(result)
    result.verification = "fixed" if verdict2 else "fixed:second review skipped"
    return _finish(result, second, "llm")


async def _review(result: PipelineResult, question: str, entities: dict, text: str, progress: Progress,
                  attempt: int = 1) -> Verdict | None:
    """The reviewer's verdict, or None if the reviewer could not be reached or replied unusably (fail open: the
    answer has already passed every deterministic guard; the event is logged)."""
    progress("verifying")
    t0 = time.monotonic()
    try:
        verdict, note = await review.review_answer(question, entities, result.sql, result.columns, result.rows, text,
                                                   call=verify_answer)
    except Exception as exc:  # noqa: BLE001
        _log.warning("reviewer unavailable for %r: %s", question[:200], exc)
        return None
    finally:
        result.timing_ms[f"review_ms_{attempt}"] = _ms(t0)
    if note:
        _log.warning("review of %r: %s", question[:200], note)
        result.review_notes.append(note)
    return verdict


def _unverified(result: PipelineResult) -> PipelineResult:
    """Nothing the reviewer doubted is shown: no figure, table or chart."""
    result.verification = "rejected"
    result.columns, result.rows, result.table, result.chart, result.notes = [], [], None, None, []
    return _finish(result, REFUSALS["unverified"], "llm_unverified", False)


async def _execute_plan(result: PipelineResult, question: str, entities: dict, db: ReadOnlyDB, max_rows: int,
                        timeout_seconds: float, progress: Progress, prompt: str, plan) -> str | PipelineResult:
    """Guard, run and write up one plan (with one repair round on a guard or SQL failure). Returns the answer text,
    or a finished refusal."""
    timing = result.timing_ms
    sql, template, chart = plan.sql, plan.answer_template, plan.chart_data
    qr, last_error, blocked = None, "", False
    for attempt in range(2):  # first try + one repair round fed the exact error
        t0 = time.monotonic()
        progress("checking_sql")
        try:
            safe_sql = validate_readonly_sql(sql, entities)
            if guardrails.company_filter_unrequested(question, safe_sql):
                raise UnsafeSQLError("do not filter on company_code or company_name: 'KPCL' means all 33 stations, "
                                     "including YTPS (RPCL) and YCCPP (KPCGPCL), which are KPCL subsidiaries")
            timing[f"guard_ms_{attempt}"] = _ms(t0)
            result.sql = safe_sql
            t0 = time.monotonic()
            progress("running_query")
            qr = await db.run(safe_sql, max_rows=max_rows, timeout_seconds=timeout_seconds, restricted=True)
            timing[f"db_ms_{attempt}"] = _ms(t0)
            bad = guardrails.implausible_result(qr.columns, qr.rows)
            if bad:
                qr = None
                raise UnsafeSQLError(f"the result is impossible: {bad}; check the formula (ratios are SUM/SUM x 100)")
            break
        except UnsafeSQLError as exc:
            last_error, blocked = f"Rejected by the SQL guard: {exc}", False
        except RestrictedQueryError as exc:
            last_error, blocked = f"Access denied: {exc}. Read only the mart_* tables listed in the schema.", True
        except Exception as exc:  # noqa: BLE001 -- SQL error or timeout
            last_error, blocked = str(exc), False
        timing[f"db_ms_{attempt}"] = _ms(t0)
        if attempt == 0:
            t0 = time.monotonic()
            progress("repairing")
            try:
                plan = await generate_query_plan(repair_prompt(prompt, sql, last_error))
            except Exception as exc:  # noqa: BLE001
                timing["repair_llm_ms"] = _ms(t0)
                last_error = f"repair call failed: {exc}"
                break
            timing["repair_llm_ms"] = _ms(t0)
            if not plan.answerable:
                return _finish(result, guardrails.safe_refusal(plan.clarification_message), "llm_declined", False)
            sql, template, chart = plan.sql, plan.answer_template, plan.chart_data

    if qr is None:
        _log.warning("query failed for %r: %s", question[:200], last_error)
        result.error = last_error
        return _finish(result, REFUSALS["not_in_data"], "llm_sql_failed" + (":blocked" if blocked else ""), False)

    if guardrails.subject_mismatch(question, result.sql):
        _log.warning("subject mismatch for %r; SQL %s", question[:200], result.sql)
        return _finish(result, REFUSALS["mismatch"], "llm_mismatch", False)

    t0 = time.monotonic()
    progress("writing_answer")
    result.columns, result.rows, result.truncated = qr.columns, qr.rows, qr.truncated
    result.table = writer.result_table(qr.columns, qr.rows)
    result.chart = None
    if chart:
        result.chart = writer.build_chart(chart.type, chart.label_column, chart.value_column, chart.series_label,
                                          qr.columns, qr.rows)
    text = writer.fill_template(template, qr.columns, qr.rows, entities,
                                preferred=chart.value_column if chart else None)
    text = writer.present(text)
    together = writer.combined_total(question, qr.columns, qr.rows)
    if together:
        text += "\n\n" + together
    result.notes = writer.caveats(result.sql, qr.columns, qr.rows, entities)
    if qr.truncated:
        result.notes.append(f"Only the first {max_rows} rows are shown.")
    if result.notes:
        text += "\n\n" + " ".join(result.notes)
    timing["fill_ms"] = _ms(t0)
    return text
