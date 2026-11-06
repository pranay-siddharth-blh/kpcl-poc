"""
Second model pass: an independent reviewer checks every model-written answer before it is shown.

The reviewer is the same gateway model, and in live testing a free-form reviewer invented objections (it demanded the
company_code filter the rules forbid, objected to fy = '2022-23' for FY 2022-23, complained about rounding). So:

  1. It is given the documentation of the columns the SQL uses, the data rules, the question, the recognised
     entities, the SQL, the result rows and the answer text.
  2. It must name ONE error category: wrong_measure, wrong_scope, wrong_period, wrong_aggregation or
     unsupported_claim. Anything else (style, rounding, wording) counts as ok.
  3. Deterministic veto: a rejection is overruled when the code can prove the claim false --
       wrong_scope      the SQL filters on every station / unit / category named in the question;
       wrong_period     the SQL filters on every period named in the question;
       wrong_measure    the SQL uses every column of the registry formula of the measure named in the question;
       unsupported_claim every figure in the answer comes from the result rows (or a column total);
       any category     the objection asks for a company_code filter, which the rules forbid.
A rejection that survives the veto gets one repair round in app.pipeline; an answer rejected again is not shown.
"""
from __future__ import annotations

import re

from app import llm
from app.llm import Verdict
from app.prompts import VERIFY_RULES
from app.semantic_layer import load_semantic_layer

CATEGORIES = ("wrong_measure", "wrong_scope", "wrong_period", "wrong_aggregation", "unsupported_claim")
_NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_DATE_LIKE_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b|\b(19|20)\d{2}-\d{2}(-\d{2})?\b|\bFY ?\d{4}-\d{2}\b|\bQ[1-4]\b")


# ---------------------------------------------------------------------------------------------- prompt
def _column_docs(sql: str) -> str:
    """Docs of the mart columns the SQL actually uses, so the reviewer does not guess what a column means."""
    tables = load_semantic_layer()["tables"]
    used_tables = set(re.findall(r"\bmart_\w+", sql or ""))
    words = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", (sql or "").lower()))
    lines = []
    for t in sorted(used_tables):
        info = tables.get(t)
        if not info:
            continue
        lines.append(f"{t} -- {info['grain']}")
        for c, ci in info["columns"].items():
            if c in words:
                unit = f" [{ci['unit']}]" if ci.get("unit") else ""
                lines.append(f"  {c}{unit}: {(ci.get('doc') or '')[:160]}")
    return "\n".join(lines)


def build_prompt(question: str, entities: dict, sql: str, columns: list[str], rows: list[tuple], answer: str) -> str:
    import json
    shown = rows[:25]
    table = "\n".join([" | ".join(columns)] + [" | ".join("" if v is None else str(v) for v in r) for r in shown])
    more = f"\n({len(rows) - len(shown)} more rows not shown)" if len(rows) > len(shown) else ""
    return (
        "You review an answer written by another assistant over KPCL's power-generation database before a government "
        "official sees it. The SQL author knows the schema below; do not second-guess a table or column that the "
        "documentation shows is right. The answer text is written automatically from the result rows.\n\n"
        + VERIFY_RULES + "\n\n## Columns used by the SQL\n" + _column_docs(sql) + "\n\n"
        "Reject ONLY for one of these clear errors:\n"
        "- wrong_measure: the SQL measures a different thing than the question asks;\n"
        "- wrong_scope: the SQL is for a different station / unit / category / reason than asked, or leaves part out;\n"
        "- wrong_period: the SQL is for a different period than asked;\n"
        "- wrong_aggregation: e.g. one row per day when a single total for the period was asked, or a sum of a level;\n"
        "- unsupported_claim: a figure or claim in the answer text is not in the result rows.\n"
        "Wording, rounding, ordering, extra detail, style or a missing chart are NEVER errors. If unsure, the answer is ok.\n\n"
        f"## Question\n{question}\n\n## Entities identified in the question\n{json.dumps(entities, ensure_ascii=False)}\n\n"
        f"## SQL\n{sql}\n\n## Result ({len(rows)} rows)\n{table}{more}\n\n## Answer text\n{answer}\n\n"
        'Reply with ONE JSON object only: {"ok": true, "category": "none", "problem": ""} or '
        '{"ok": false, "category": "<one of: ' + ", ".join(CATEGORIES) + '>", "problem": "<one sentence>"}'
    )


# ---------------------------------------------------------------------------------------------- deterministic facts
def _quoted(sql: str) -> set[str]:
    return {m.upper() for m in re.findall(r"'([^']*)'", sql or "")}


def scope_verified(entities: dict, sql: str) -> bool | None:
    names = [*entities.get("stations", []), *entities.get("units", []), *entities.get("categories", []),
             *entities.get("reasons", []), *entities.get("suppliers", []),
             *[r.upper() for r in entities.get("reservoirs", [])]]
    if not names:
        return None
    q = _quoted(sql)
    return all(n.upper() in q for n in names)


def period_verified(entities: dict, sql: str) -> bool | None:
    periods = [p for p in entities.get("periods", []) if p["kind"] != "out_of_range"]
    if not periods:
        return None
    q, low = _quoted(sql), (sql or "").lower()
    for p in periods:
        k, v = p["kind"], str(p["value"]).upper()
        if k in ("fy", "water_year", "date") and v not in q:
            return False
        if k == "month":
            y, mon = v.split("-")[1], v.split("-")[0]
            mm = f"{['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'].index(mon) + 1:02d}"
            if v not in q and f"{y}-{mm}" not in q and f"{y}-{mm}-01" not in q:
                return False
        if k == "quarter" and not (v in q and (p.get("fy") or "") in q):
            return False
        if k in ("calendar_year", "season_cy"):
            y = v if k == "calendar_year" else str(p["fy"])
            if f"{y}-01-01" not in q and f"'{y}'" not in low:
                return False
    return True


def measure_verified(entities: dict, sql: str) -> bool | None:
    keys = entities.get("metrics", [])
    if not keys:
        return None
    metrics = {m["key"]: m for m in load_semantic_layer()["metrics"]}
    low = (sql or "").lower()

    def columns_of(e: dict) -> set[str]:
        return {c for part in (e.get("num", ""), e.get("den", "")) for c in re.findall(r"\b[a-z]+_[a-z0-9_]+\b", part)}
    for k in keys:
        m = metrics.get(k)
        if not m:
            return None
        forms = [columns_of(e) for e in m["expr"].values() if columns_of(e)]
        # verified when the SQL uses every column of at least one of the metric's reviewed formulas
        if not any(all(re.search(r"\b" + c + r"\b", low) for c in cols) for cols in forms):
            return False
    return True


def claims_supported(answer: str, question: str, columns: list[str], rows: list[tuple]) -> bool:
    """Every figure in the answer is a result value, a column total, or a number already in the question."""
    values = [float(v) for r in rows for v in r if isinstance(v, (int, float)) and not isinstance(v, bool)]
    totals = [sum(float(r[i]) for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool))
              for i in range(len(columns))]
    known = values + totals + [float(x.replace(",", "")) for x in _NUM_RE.findall(_DATE_LIKE_RE.sub(" ", question or ""))
                               if x.replace(",", "").replace(".", "").lstrip("-").isdigit()]
    for tok in _NUM_RE.findall(_DATE_LIKE_RE.sub(" ", answer or "")):
        try:
            x = float(tok.replace(",", ""))
        except ValueError:
            continue
        if abs(x) < 10 and float(x).is_integer():
            continue                       # small counts in prose ("3 units", "2 stations") are not figures to trace
        if not any(abs(x - k) <= max(abs(k) * 0.002, 0.006) for k in known):
            return False
    return True


def shape_matches(entities: dict, rows: list[tuple]) -> bool:
    """One row for a single-figure question; several rows for a comparison, list, ranking, breakdown or trend."""
    many_asked = bool(set(entities.get("intents", [])) & {"ranking", "comparison", "breakdown", "trend"}) or \
        len(entities.get("periods", [])) > 1 or len(entities.get("stations", [])) + len(entities.get("units", [])) > 1
    return len(rows) > 1 if many_asked else len(rows) == 1


def veto(verdict: Verdict, category: str, question: str, entities: dict, sql: str, columns: list[str],
         rows: list[tuple], answer: str) -> str | None:
    """Why a rejection is provably wrong, or None if it stands."""
    if re.search(r"company_code|company_name|company code", verdict.problem, re.I):
        return "the objection asks for a company filter, which the rules forbid"
    if category not in CATEGORIES:
        return "not one of the error categories"
    if category == "wrong_scope" and scope_verified(entities, sql):
        return "the SQL filters on every station / unit / category / supplier / reason named in the question"
    if category == "wrong_period" and (not entities.get("periods") or period_verified(entities, sql)):
        return ("the question names no period" if not entities.get("periods")
                else "the SQL filters on every period named in the question")
    if category == "wrong_measure" and measure_verified(entities, sql):
        return "the SQL uses the registry formula of the measure named in the question"
    if category == "wrong_aggregation" and shape_matches(entities, rows):
        return "the result has the shape the question asks for (one figure, or one row per item compared or listed)"
    if category == "unsupported_claim" and (len(rows) > 1 or claims_supported(answer, question, columns, rows)):
        return ("a multi-row answer is written from the rows by the deterministic narrator, not by the model"
                if len(rows) > 1 else "every figure in the answer comes from the result row")
    return None


async def review_answer(question: str, entities: dict, sql: str, columns: list[str], rows: list[tuple],
                        answer: str, call=None) -> tuple[Verdict, str]:
    """(verdict after the veto, note). Raises if the reviewer cannot be reached (the caller fails open)."""
    raw = await (call or llm.verify_answer)(build_prompt(question, entities, sql, columns, rows, answer))
    if raw.ok:
        return raw, ""
    category = (raw.category or "").strip().lower()
    reason = veto(raw, category, question, entities, sql, columns, rows, answer)
    if reason:
        return Verdict(True, ""), f"reviewer overruled ({category}: {raw.problem[:160]}) because {reason}"
    return raw, f"{category}: {raw.problem}"
