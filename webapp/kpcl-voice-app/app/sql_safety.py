"""
Checks on model-written SQL before it reaches the database. Two kinds, both fail closed (a query that can't be
shown safe is refused, and its error goes back to the model for one repair attempt):

  1. Read-only shape (as in the WB and Cescom POCs): one SELECT / WITH statement, no write or admin keyword,
     no unresolved {placeholder}, only mart_* tables (the authorizer in app.db enforces this again at compile
     time, so this check exists to give the model a clear repair message).
  2. KPCL grain guards -- the ways a clean-looking query silently returns a wrong number on this data:
       a. averaging or summing a ratio / level column (PLF, PAF, aux %, GCV, heat rate, days of stock, water
          level ...) instead of SUM(numerator)/SUM(denominator);
       b. joining two generation facts of different grain in one aggregate (unit-day with station-month etc.):
          the join repeats rows and double counts (UNION ALL of separate queries is fine);
       c. joining the reason lookup without saying which reason family (outage vs back-down);
       d. reading the point-in-time status table for a question about a past period.
The rewrite _nulls_last() then makes ascending sorts put missing values last (SQLite sorts NULL first).
"""
from __future__ import annotations

import re

from app.semantic_layer import load_semantic_layer

_FORBIDDEN = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "ATTACH", "DETACH", "PRAGMA",
              "VACUUM", "REINDEX", "ANALYZE", "GRANT", "REVOKE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"]
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b|\bREPLACE\s+INTO\b|\bRECURSIVE\b", re.IGNORECASE)
_START_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)\s*(\()?", re.IGNORECASE)
_CTE_RE = re.compile(r"(?:\bWITH\b|,)\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.IGNORECASE)
_AGG_COL_RE = re.compile(r"\b(AVG|SUM|TOTAL)\s*\(\s*(?:DISTINCT\s+)?(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\s*\)", re.IGNORECASE)
_GEN_FACTS = ("mart_fact_unit_generation_daily", "mart_fact_unit_generation_monthly", "mart_fact_station_generation_daily",
              "mart_fact_station_generation_monthly", "mart_fact_station_generation_fy")
_TABLE_FUNCTIONS = {"json_each", "json_tree"}


class UnsafeSQLError(ValueError):
    """The SQL is not a safe / correct query for this data. The message is written for the model's repair turn."""


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)


def _strip_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_readonly_sql(sql: str, entities: dict | None = None) -> str:
    """Return the cleaned SQL (execute that, not the input) or raise UnsafeSQLError."""
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty SQL.")
    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    code = _strip_strings(cleaned)
    if ";" in code:
        raise UnsafeSQLError("Multiple statements are not allowed; return one SELECT.")
    if not _START_RE.match(code):
        raise UnsafeSQLError("Only a single SELECT (or WITH ... SELECT) statement is allowed.")
    m = _PLACEHOLDER_RE.search(cleaned)  # string literals included: '{station}' in a WHERE is the classic leak
    if m:
        raise UnsafeSQLError(f"The SQL contains the template placeholder {m.group(0)}; put the literal value in the SQL. "
                             "Curly-brace placeholders belong only in answer_template.")
    m = _FORBIDDEN_RE.search(code)
    if m:
        raise UnsafeSQLError(f"Forbidden keyword in SQL: {m.group(0)!r}. Only read-only SELECT queries are allowed.")
    ctes = {c.lower() for c in _CTE_RE.findall(code)}
    for name, paren in _TABLE_REF_RE.findall(code):
        low = name.lower()
        if low.startswith("mart_") or low in ctes or (paren and low in _TABLE_FUNCTIONS) or low in ("select",):
            continue
        raise UnsafeSQLError(f"Table {name!r} is not available. Use only the mart_* tables listed in the schema.")
    check_grain(code, entities or {})
    check_kpi_formulas(code)
    return _nulls_last(cleaned)


# A column the model names after a KPI must be computed with that KPI's formula (registry): an "aux_pct" that
# divides aux by aux, or a "plf_pct" over declared capacity, is a clean-looking wrong answer otherwise.
_KPI_ALIASES = {
    "plf_pct": ("gross_gen_mu", "capacity_mu"), "plf": ("gross_gen_mu", "capacity_mu"),
    "paf_pct": ("declared_capacity_mu", "capacity_mu"), "paf": ("declared_capacity_mu", "capacity_mu"),
    "plf_with_nld_pct": ("deemed_gen_mu", "capacity_mu"),
    "aux_pct": ("aux_consumption_mu", "gross_gen_mu"), "aux_consumption_pct": ("aux_consumption_mu", "gross_gen_mu"),
    "sp_coal_kg_kwh": ("coal_consumed_mt", "gross_gen_mu"), "specific_coal_kg_kwh": ("coal_consumed_mt", "gross_gen_mu"),
    "sp_oil_ml_kwh": ("gross_gen_mu",), "cea_target_achievement_pct": ("gross_gen_mu", "cea_target_mu"),
    "kpcl_share_of_consumption_pct": ("state_consumption_mu",), "kpcl_share_pct": ("state_consumption_mu",),
}


def _select_expression(code: str, alias_pos: int) -> str:
    """The expression that ends right before 'AS <alias>' (walk back to the comma / SELECT at depth 0)."""
    depth, i = 0, alias_pos - 1
    while i >= 0:
        ch = code[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        elif depth == 0 and re.match(r"(?i)select\b", code[i:i + 7]) and (i == 0 or not code[i - 1].isalnum()):
            i += 6
            break
        i -= 1
    return code[i + 1:alias_pos]


def check_kpi_formulas(code: str) -> None:
    for m in re.finditer(r"\bAS\s+([A-Za-z_]\w*)", code, re.IGNORECASE):
        need = _KPI_ALIASES.get(m.group(1).lower())
        if not need:
            continue
        expr = _select_expression(code, m.start())
        if "(" not in expr:  # a bare column read (e.g. f.plf_pct AS plf_pct) or a CTE alias carried through
            continue
        missing = [c for c in need if not re.search(r"\b" + c + r"\b", expr)]
        if missing and not re.search(r"\b" + m.group(1) + r"\b", expr):
            raise UnsafeSQLError(
                f"The column {m.group(1)} is not computed with its formula (it must use {', '.join(need)}). "
                "Use the metric formulas listed in the prompt, e.g. aux % = SUM(aux_consumption_mu)/SUM(gross_gen_mu)*100, "
                "PLF = SUM(gross_gen_mu)/SUM(capacity_mu)*100, PAF = SUM(declared_capacity_mu)/SUM(capacity_mu)*100.")


def check_grain(code: str, entities: dict) -> None:
    layer = load_semantic_layer()
    no_avg = set(layer["non_averageable_columns"])
    no_sum = set(layer["non_summable_columns"])
    for fn, col in _AGG_COL_RE.findall(code):
        fn_u, col_l = fn.upper(), col.lower()
        if fn_u == "AVG" and col_l in no_avg or fn_u in ("SUM", "TOTAL") and col_l in no_sum:
            raise UnsafeSQLError(
                f"{fn_u}({col}) is wrong on this data: {col} is a ratio or a level, so averaging or adding it across "
                "rows gives a wrong figure. Compute the ratio as SUM(numerator)/SUM(denominator) over the rows in "
                "scope (e.g. PLF = SUM(gross_gen_mu)/SUM(capacity_mu)*100, weighted GCV = SUM(gcv_coal_x_coal_mt)/"
                "SUM(coal_mt_with_gcv)), or read the pre-computed column of a single row. For levels such as "
                "days_of_stock or water_level take the value on a date, or MIN/MAX/AVG over days.")
    low = code.lower()
    facts = {f for f in _GEN_FACTS if re.search(r"\b" + f + r"\b", low)}
    if len(facts) > 1 and not re.search(r"\bunion\b", low):
        raise UnsafeSQLError(
            "The query joins generation facts of different grain (" + ", ".join(sorted(facts)) + "); a join repeats "
            "rows and double counts. Use one fact at the grain of the question, or put each figure in its own "
            "SELECT combined with UNION ALL.")
    if "mart_dim_outage_reason" in low and "reason_class" not in low:
        raise UnsafeSQLError("mart_dim_outage_reason holds two reason families; filter reason_class = 'OUTAGE' or "
                             "'BACKDOWN' (the facts already carry reason_description, so the join is usually unnecessary).")
    past = [p for p in entities.get("periods", []) if p.get("kind") not in ("out_of_range",) and not p.get("relative")]
    if "mart_fact_unit_current_status" in low and past and "mart_fact_unit_outage" not in low:
        raise UnsafeSQLError("mart_fact_unit_current_status only describes the end of the data (30-Apr-2025); for a "
                             "past period use mart_fact_unit_outage (outages that started, or were open, in that period).")


# ---------------------------------------------------------------------------------------------- NULLS LAST
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_ORDER_END_RE = re.compile(r"\b(LIMIT|OFFSET|UNION|EXCEPT|INTERSECT|WINDOW)\b", re.IGNORECASE)


def _nulls_last(sql: str) -> str:
    """Every ascending ORDER BY term gets NULLS LAST, so 'lowest' never names a row that has no figure
    (port of the WB POC rewrite; skips terms inside OVER(...) windows only if they already say NULLS)."""
    out, pos = [], 0
    for m in _ORDER_BY_RE.finditer(sql):
        if m.start() < pos:
            continue
        start = i = m.end()
        depth = 0
        while i < len(sql):
            ch = sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            elif depth == 0 and _ORDER_END_RE.match(sql, i) and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                break
            i += 1
        terms, depth, cur = [], 0, ""
        for ch in sql[start:i]:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                terms.append(cur)
                cur = ""
            else:
                cur += ch
        terms.append(cur)
        fixed = []
        for t in terms:
            body = t.rstrip()
            if body and not re.search(r"\bNULLS\b", body, re.I) and not re.search(r"\bDESC$", body, re.I):
                body += " NULLS LAST"
            fixed.append(body + t[len(t.rstrip()):])
        out.append(sql[pos:start] + ",".join(fixed))
        pos = i
    return "".join(out) + sql[pos:]
