"""
Builds the single prompt sent to the model per question, within a token budget (the gateway model has a 32k
context; the prompt is kept to settings.kpcl_prompt_token_budget, estimated at 4 characters per token):

  1. fixed instructions (KPCL rules, output contract);
  2. the schema of only the mart tables relevant to the question (from the semantic layer, with grain, units
     and column docs) -- never core_/raw_/stg_ tables, which the model may not read anyway;
  3. the formulas of the metrics named in the question, the glossary terms used, the always-on rules;
  4. the worked examples most similar to the question;
  5. the verified entities (codes, periods, data end) extracted deterministically.
If the budget is exceeded, examples are dropped first, then column docs are shortened.
"""
from __future__ import annotations

import json
import re

from app.config import settings
from app.semantic_layer import norm
from app.verified_queries import example_questions, example_sql

SYSTEM_INSTRUCTIONS = """\
You write SQL for a read-only question-answering assistant over KPCL's Generation Management System (GMS) data: \
33 power stations (thermal, gas, waste-to-energy, hydro, wind, solar), 103 units, daily data from 01-Apr-2015 to \
30-Apr-2025, stored in SQLite. The data is a synthetic POC dataset built around real figures from the K-eRP reports.

You get ONE turn. Return a single JSON object with:
  answerable       true, or false when the question can't be answered correctly from these tables;
  sql              ONE read-only SQLite SELECT (or WITH ... SELECT) over the mart_* tables below. Every value baked in
                   as a literal; give EVERY selected column an alias with AS;
  answer_template  one or two plain sentences answering the question, with {alias} placeholders that Python fills
                   from your query's FIRST row after running it (you will NOT see the result). Only use aliases from
                   your SELECT or keys from the entities block. Never put numbers you guessed in it. Write units
                   after placeholders only if the alias has no unit suffix (the filler already appends MU, %, etc.);
  clarification_message  only when answerable is false: one short sentence about what the data lacks;
  chart_data       null, or {"type": "bar"|"line", "label_column": alias, "value_column": alias, "series_label": text}
                   for multi-row comparisons ("bar") or trends over time ("line").

Hard rules (breaking one makes the answer wrong):
- Only mart_* tables and only columns listed below. SQLite dialect: no FROM dual, no ILIKE, date maths with date() / \
strftime(); integer division truncates, so multiply by 1.0 first.
- Ratios are always SUM(numerator)/SUM(denominator) over the rows in scope (formulas below). Never AVG or SUM a \
*_pct, sp_*, wa_*, gcv_*, heat-rate, days_of_stock or water_level column across rows; a pre-computed ratio column may \
be read as-is only for one row of its own grain.
- Use one generation fact at the grain of the question; never join two generation facts (they double count). For \
side-by-side figures from different grains use separate SELECTs combined with UNION ALL.
- Station codes, unit codes, category labels, fy ('2024-25'), month_label ('MAR-2025'), water_year and dates must \
be exactly as in the entities block or the schema. Text values are upper case where the schema says so.
- 'Now', 'currently', 'latest', 'today' mean the end of the data, 30-Apr-2025. FY 2025-26 contains April 2025 only.
- Rankings: exclude NULLs and use ORDER BY ... LIMIT. Return one row per entity unless a breakdown or trend is asked.
- If the question needs data not listed below (billing, JMR, defects, hourly readings, KERC targets, HR), set \
answerable=false. Never answer from general knowledge.
"""


def _approx_tokens(text: str) -> int:
    return len(text) // 4


def schema_block(tables: dict, full_docs: bool = True) -> str:
    lines = []
    for name, t in tables.items():
        lines.append(f"TABLE {name}  -- grain: {t['grain']}. {t['doc'] if full_docs else ''}".rstrip())
        for c, info in t["columns"].items():
            unit = f" [{info['unit']}]" if info.get("unit") else ""
            doc = f" -- {info['doc']}" if full_docs and info.get("doc") else ""
            lines.append(f"  {c} {info['type']}{unit}{doc}")
        lines.append("")
    return "\n".join(lines)


def metrics_block(metrics: list[dict]) -> str:
    lines = []
    for m in metrics:
        forms = []
        for fam, e in m["expr"].items():
            f = e["num"] if "den" not in e else f"{e['num']} * {e.get('scale', 1)} / NULLIF({e['den']}, 0)"
            where = {"all": "", "daily": " (daily facts)", "rollup": " (monthly / FY facts)"}[fam]
            forms.append(f"{f}{where}")
        src = f" from {m['source_table']}" + (f" WHERE {m['filter']}" if m.get("filter") else "") if m.get("source_table") else ""
        lines.append(f"- {m['label']} [{m['unit']}]: " + "; ".join(forms) + src)
    return "\n".join(lines)


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "of", "in", "and", "a", "an", "is", "are", "how", "many", "what", "which", "for", "to", "on", "by",
         "with", "was", "were", "did", "does", "do", "from", "that", "have", "has", "per", "or", "as", "at", "be", "it"}


def select_examples(question: str, limit: int) -> list[tuple[str, str]]:
    q = {w for w in _WORD_RE.findall(norm(question)) if w not in _STOP}
    questions, sqls = example_questions(), example_sql()
    scored = []
    for n, text in questions.items():
        words = set(_WORD_RE.findall((text + " " + sqls[n]).lower().replace("_", " ")))
        scored.append((len(q & words), -n, n))
    keep = sorted(n for _, _, n in sorted(scored, reverse=True)[:limit])
    return [(questions[n], sqls[n]) for n in keep]


def build_prompt(question: str, pruned: dict, entities: dict, max_examples: int = 8) -> str:
    budget = settings.kpcl_prompt_token_budget
    ent = {k: v for k, v in entities.items() if k != "intents"}
    ent["data_as_of"] = pruned["data_period"]["data_as_of"]
    for full_docs, n_examples in ((True, max_examples), (True, 4), (False, 4), (False, 0)):
        parts = [
            SYSTEM_INSTRUCTIONS,
            "\n## Tables you may query (SQLite)\n" + schema_block(pruned["tables"], full_docs),
            "\n## Metric formulas (use exactly these)\n" + metrics_block(pruned["metrics"]),
            "\n## Rules for this data\n" + "\n".join(f"- {r}" for r in pruned["rules"]),
        ]
        if pruned["glossary"]:
            parts.append("\n## Terms in the question\n" + "\n".join(f"- {k}: {v}" for k, v in pruned["glossary"].items()))
        if n_examples:
            ex = select_examples(question, n_examples)
            parts.append("\n## Worked examples (reviewed SQL for similar questions)\n" +
                         "\n\n".join(f"-- Q: {q}\n{s};" for q, s in ex))
        parts.append("\n## Entities already identified in the question (verified codes; the question may name more)\n"
                     + json.dumps(ent, ensure_ascii=False))
        if ent.get("suppliers"):
            parts.append("\n\"suppliers\" are exact supplier_code values of mart_fact_coal_receipt (IMPORTED = is_imported = 1).")
        if ent.get("reasons"):
            parts.append("\n\"reasons\" are exact values of the reason_code column in mart_fact_generation_loss_daily "
                         "(back-down losses) and mart_fact_unit_outage (outages); filter on reason_code with them.")
        parts.append(f"\n## Question\n{question}\n")
        prompt = "".join(parts)
        if _approx_tokens(prompt) <= budget:
            return prompt
    return prompt


def coverage_repair_prompt(prompt: str, refusal: str) -> str:
    """The model declined by claiming a period is outside the data when it is not."""
    return (prompt + "\n\n## Your previous answer was wrong\nYou declined with: \"" + refusal[:400] + "\"\n"
            "That is not true. The data has every day from 01-Apr-2015 to 30-Apr-2025 in every fact table, so every "
            "financial year FY 2015-16 to FY 2024-25 is complete and April 2025 is present. Return answerable=true with "
            "SQL that answers the question, unless it truly needs data that is not in the schema above.\n")


def repair_prompt(prompt: str, bad_sql: str, error: str) -> str:
    return (prompt + "\n\n## Your previous answer failed\nThe SQL you returned was:\n```sql\n" + bad_sql + "\n```\n"
            "It was rejected or failed with:\n" + error + "\n\nReturn a corrected JSON object (same keys) whose SQL fixes "
            "exactly this problem. Check every table and column name against the schema above.\n")


# ---------------------------------------------------------------------------------------------- answer verification
VERIFY_RULES = """Facts about the data (use them to judge):
- KPCL means ALL 33 stations, including YTPS (company RPCL) and YCCPP (company KPCGPCL); a filter on company_code that the
  question did not ask for is an error. KPCL has 103 units and 9,119.8 MW installed.
- Data runs 01-Apr-2015 to 30-Apr-2025. fy is text like '2024-25' (April-March). water_year runs June-May. 'Now' means
  30-Apr-2025. 'Last month' means March 2025.
- Ratios are recomputed from sums, never averaged: PLF % = SUM(gross_gen_mu)/SUM(capacity_mu)x100; PAF % =
  SUM(declared_capacity_mu)/SUM(capacity_mu)x100; aux % = SUM(aux_consumption_mu)/SUM(gross_gen_mu)x100; CEA target
  achievement % = SUM(gross_gen_mu)/SUM(cea_target_mu)x100; specific coal kg/kWh = SUM(coal_consumed_mt)/1000/SUM(gross_gen_mu).
- Levels and balances (water_level, capacity_pct, closing_stock_mt, days_of_stock, max_day_gen_mu) are never summed.
- A single figure for a month, quarter, FY or water year is one total over the whole period, not one row per day.
- Generation is in MU (million units = GWh). Coal in tonnes. Reservoir levels in each reservoir's own unit (feet or metres).
- Back-down losses are in mart_fact_generation_loss_daily; outages in mart_fact_unit_outage; reasons are reason_code values."""


def verify_repair_prompt(prompt: str, bad_sql: str, problem: str) -> str:
    return (prompt + "\n\n## A reviewer rejected your previous answer\nThe SQL you returned was:\n```sql\n" + bad_sql +
            "\n```\nThe reviewer found: " + problem + "\n\nReturn a corrected JSON object (same keys) that fixes exactly "
            "this problem. If the reviewer is wrong, return the same SQL again.\n")
