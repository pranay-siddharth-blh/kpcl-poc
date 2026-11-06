# Layered NL2SQL Architecture — Reference Blueprint

**Audience:** an engineer or coding agent building (or improving) a natural-language-to-SQL assistant over **any** dataset.
**Source:** distilled from a working, tested system (the "KPCL GMS assistant": 10 years of daily power-generation data,
SQLite warehouse, FastAPI app, 548 offline tests, 213/213 live stress questions answered with figures checked against the
database). KPCL details appear only as **worked examples**; every rule here is written to transfer to other data.

> How to use this file: drop it into your project, read §1–§3 once, then follow §14 ("Adoption plan") layer by layer.
> Each layer section ends with a **Contract** (what it must provide to the layer above) and a **Checklist** you can tick.

---

## 0. Contents

1. Core principles (the ten rules everything else follows)
2. The layer map
3. Warehouse layers: L0 sources → L1 raw → L2 staging → L3 core → L4 reconcile → L5 mart
4. L6 Semantic layer (metadata that drives the AI)
5. L7 Ledger and build checks
6. L8 Database access layer (how the app connects to the DB)
7. L9 AI layer — the question pipeline
8. L9a Guardrails
9. L9b Deterministic routes (no LLM)
10. L9c The LLM path: prompt, output contract, SQL guard, execution, writer, reviewer
11. L10 Service / API layer
12. L11 Front end
13. L12 Reports and exports (optional)
14. Testing and evaluation
15. Adoption plan for an existing NL2SQL bot
16. Postgres / other-engine equivalents
17. Appendix: file formats, skeletons, folder layout

---

## 1. Core principles

1. **The model never touches raw data.** It may read only a curated, documented, query-ready layer (the *mart*). Everything
   below the mart exists to make the mart correct and traceable.
2. **The model never types a number the user sees.** It writes SQL and an answer *template* with `{placeholders}`; Python
   runs the SQL and fills the template from the result. The model never sees the result rows.
3. **Deterministic first, LLM last.** Refusals, definitions, known reports, single-figure lookups and "data not available"
   notes are answered by code. Only what is left reaches the model — and the model gets **one** structured call
   (plus at most one repair round).
4. **Metadata is the single source of truth.** Table/column docs, metric formulas, synonyms, capability status and
   known issues live in hand-written JSON, are loaded into the warehouse, and are exported to one `semantic_layer.json`
   the app reads. Code never hard-codes a formula that the registry also defines.
5. **Ratios are SUM/SUM, never AVG of a ratio.** Every metric declares its numerator and denominator. Levels/balances
   (stock, water level, % storage) are never summed over time. A guard enforces both.
6. **Defense in depth on the database.** Read-only file/role + a compile-time authorizer (mart tables only) + a SQL text
   guard + timeout + row cap. No single layer is trusted alone.
7. **Fail closed, explain honestly.** Unsafe SQL is refused (with the error fed back once to the model for repair). Data
   the system doesn't hold gets a clear "not loaded" note, never a guess or an empty table.
8. **Every number is reproducible.** Golden figures from a trusted source ("anchors") are reconciled at build time; tests
   assert them; dashboards, chat answers and exported reports read the same queries so they can never disagree.
9. **Everything is traceable.** Every source column has recorded lineage (where it landed, or why not). Every build
   records checks and timings. Known data problems are listed with the caveat attached to affected answers.
10. **Inputs are locked.** The app opens the warehouse read-only; a SHA-256 manifest of the DB, the semantic layer and the
    example library is checked by a test, so the chatbot provably never altered what it reads.

---

## 2. The layer map

```
                         ┌──────────────────────────────────────────────────────────┐
  L11 Front end          │ dashboard · chat (SSE streaming) · report viewer         │
                         └───────────────▲──────────────────────────────────────────┘
  L10 Service/API        │ FastAPI routes · rate limit · CSP · body cap · SSE        │
                         └───────────────▲──────────────────────────────────────────┘
  L9  AI layer           │ guardrails → deterministic routes → LLM plan → SQL guard  │
                         │ → execute → result guards → writer → reviewer             │
                         └───────────────▲──────────────────────────────────────────┘
  L8  DB access          │ read-only connection · authorizer (mart_* only) ·         │
                         │ timeout · row cap · single executor                       │
                         └───────────────▲──────────────────────────────────────────┘
  L6  Semantic (export)  │ semantic_layer.json · example_queries.sql ·               │
                         │ knowledge_base.yaml · integrity_manifest.json            │
  ══════════════════════ WAREHOUSE (built offline, read-only to the app) ═══════════
  L7  ledger_*           │ lineage · known issues · reconcile log · build checks     │
  L6  semantic_*         │ table/column docs · metrics · capabilities · data period  │
  L5  mart_*             │ wide dims + facts, pre-joined, documented   ◄── AI reads   │
  L4  reconcile          │ pin to golden figures (optional)                          │
  L3  core_*             │ normalised model, PKs/FKs/CHECKs                           │
  L2  stg_*              │ typed copies + quality_flags per row                       │
  L1  raw_*              │ value-for-value TEXT copy + source row number             │
  L0  src_* / ref_*      │ file registry (sha256) · cell dumps · reference anchors   │
                         └──────────────────────────────────────────────────────────┘
```

**One physical database, table-name prefix = layer** (`src_`, `ref_`, `raw_`, `stg_`, `core_`, `mart_`, `semantic_`,
`ledger_`). In Postgres use one **schema per layer** instead (§16). The build runs as ordered, resumable stages:

```
sources → raw → staging → core → reconcile → mart → semantic → ledger → checks      (~1.5 min for 365k fact rows)
python build_warehouse.py [--from mart] [--to reconcile] [--only checks] [--db path]
```

Each stage **drops and recreates only its own prefix**, so `--from mart` rebuilds mart and everything after it without
re-reading sources. Any check failure raises and stops the build.

---

## 3. Warehouse layers

### L0 — Sources & references (`src_*`, `ref_*`)

Purpose: know exactly what came in.

| Table | Contents |
|---|---|
| `src_file` | every input file: name, role, loaded? (0/1), size, **sha256**, note (why loaded / why not) |
| `src_sheet` | every sheet/table of every file: dimensions, storage mode, which raw table it became, loaded into core? |
| `src_cell` | every non-empty cell **(value + formula)** of small/irregular sheets (READMEs, validation tabs, lookup grids) |
| `ref_<name>` | reference/ground-truth values flattened to one row per value (e.g. `ref_document_anchor(anchor_path, section, value_text, value_num)`) |

Rules:
- Record files you **don't** load too, with the reason (e.g. "second workbook uses different station names").
- Keep formulas, not just values — they document how the source author computed things.

**Contract:** an auditable list of every input and its hash.  **Checklist:** ☐ all files registered ☐ hashes stored ☐ refs flattened.

### L1 — Raw (`raw_*`)

Purpose: a lossless, query-able copy.

- One table per tabular source, **all columns TEXT**, header names snake_cased, plus `source_row_no` (Excel row / file line).
- No cleaning, no type conversion, skip only fully empty rows.
- Batch inserts (e.g. 20,000 rows) for speed.

**Contract:** any value in any later layer can be traced back to `raw_<table>.source_row_no`.

### L2 — Staging (`stg_*`)

Purpose: typed data plus data-quality signals, still one table per source.

- **Type inference per column** (record it in `stg__column_type(stg_table, column_name, inferred_type)`):
  bool (`True/False`) → date (`YYYY-MM-DD`) → datetime → int (only if the column *name* looks like an id/count AND all
  values are whole) → real → text.
- **`quality_flags` column** (JSON list per row, NULL when clean). Flags are *domain rules*, e.g.
  `heat_rate_nonpositive_nulled`, `heat_rate_outlier_low_generation`, `paf_below_plf_rounding`, `open_outage`,
  `target_missing`. Flags describe; they don't delete.

**Contract:** typed rows + per-row quality flags.  **Checklist:** ☐ inferred types stored ☐ quality rules written per table.

### L3 — Core (`core_*`)

Purpose: the normalised business model — the "truth" in third normal form.

- Written as a reviewed DDL file (`schema_core.sql`) with **PKs, FKs, CHECK constraints, generated columns**
  (e.g. `net_export = gross − aux`, `closing_stock = opening + receipt − consumed`).
- Conventions that pay off later:
  - `snake_case`, singular table names; `*_code` = business key, `*_id` = surrogate.
  - **Unit of measure is the column suffix** (`_mu`, `_mw`, `_hrs`, `_mt`, `_kl`, `_pct` 0–100, `_rs` …). The semantic
    layer reads units from suffixes, so naming *is* documentation.
  - **Base tables hold measured facts only; ratios are computed later as SUM/SUM.**
  - A calendar dimension (`dim_date`) with every business period: FY, FY month, quarter, water year/season, ISO week
    start, month label in the source system's own format (`'MAR-2025'`).
- Hard-coded small lookups (companies, categories, reason codes) live in the build script with their display labels and
  order.
- After loading: `PRAGMA foreign_key_check` (or equivalent) **must return nothing**, else fail.
- While mapping, record **lineage**: for every staging column, the core column it landed in, or `UNMAPPED (<reason>)`
  ("repeats unit.station_code", "recomputed in mart", "always SYNTHETIC").
- Tables you designed but have no data for are still created (empty) — the capability manifest (§4) declares them
  `not_loaded` so the bot says so instead of returning empty results.

**Contract:** constrained, FK-clean model + lineage map.

### L4 — Reconcile (optional but recommended)

Purpose: force the warehouse to agree with **trusted figures** ("anchors": numbers transcribed from official reports).

- Anchors live in a JSON file → `ref_*` table.
- Apply steps in a fixed order, e.g.: overwrite a documented day with the report's unit figures; rescale a month
  multiplicatively so its total equals the report (each day keeps its shape; dependent columns scale with it); then
  rescale the rest of the year with the reconciled month held fixed.
- **Verify independently**: re-query the DB fresh and compare every anchor within a tolerance (e.g. 0.01) →
  `ledger_reconciliation(check_no, label, expected, actual, diff, tolerance, status)`. Any FAIL stops the build.
- Log every action to `ledger_reconcile_log`. Record any safety adjustment as a known issue (e.g. "hours set to 0 so the
  day still adds to 24").

KPCL example: 71 anchor checks, 71 PASS; every rescale factor ×1.0000 (the source already matched), only one day changed.

### L5 — Mart (`mart_*`) — the only layer the AI may read

Purpose: few, wide, documented tables an LLM can query without joins or traps.

Design rules (each fixes a real failure seen with LLM SQL):

1. **Physical tables, built from core** (not views over 300k-row facts — speed and stable plans).
2. **Denormalise date and entity attributes onto every fact**: `fy`, `year_month`, `month_label`, `fy_quarter`, `season`,
   `water_year`, `station_key`, `category_label`, `plant_type`… so most questions need **no join**.
3. **One table per grain**, named by grain: `fact_unit_generation_daily`, `…_monthly`, `fact_station_generation_daily`,
   `…_monthly`, `…_fy`. Each has a comment line `-- grain: station x month`.
4. **Roll-ups carry pre-computed ratios for their own row** (PLF %, aux %…) **plus the numerator/denominator helper sums**
   (`capacity_mu`, `gcv_x_coal_mt`, `coal_mt_with_gcv`…) so any re-aggregation stays correct.
5. **"Now" is the data end date**, substituted at build time (`{{DATA_AS_OF}}`), never the wall clock.
6. **Point-in-time tables are separate** (`fact_unit_current_status`, `fact_unit_cumulative`) and documented as "as of
   the data end" so they're not used for past periods.
7. **Provenance per row:** e.g. `anchor_scope` = `document_day | document_month_total | document_fy_total |
   workbook_estimate | NULL(synthetic)`; a roll-up keeps a scope only if every row under it has the same one. Answers can
   then say "this figure is pinned to the official report".
8. **Don't build facts over empty core tables**; the capability manifest covers them.
9. Dimensions get display helpers: labels, sort order, counts (`unit_count`), relationships (`fed_by_reservoirs`).

KPCL example: 22 mart tables (8 dimensions, 14 facts).

**Contract:** a small set of documented tables, each with one grain, where every needed column exists and every ratio
can be computed as SUM/SUM.

---

## 4. L6 — Semantic layer

Hand-written metadata in `warehouse/metadata/` → loaded into `semantic_*` tables by the build (validated against the real
mart) → exported by a separate script to **`semantic_layer.json`**, the only file the app reads (cached in memory).

### 4.1 Metadata files (single source of truth)

| File | Holds | Build fails if… |
|---|---|---|
| `semantic_docs.json` | per mart table: `group`, `grain`, `doc`, `date_column`, per-column docs; `common_columns` docs; `unit_suffixes` (suffix → [unit, additive 0/1]); entity **aliases** (stations, reservoirs, categories); **glossary** (term → aliases + plain definition); **rules** (always-on prompt rules) | any mart table or column is undocumented |
| `metric_registry.json` | every KPI: key, label, unit, precision, kind (sum/ratio/level), **synonyms**, `expr` per table family (`{"num": "SUM(gross_gen_mu)", "den": "SUM(capacity_mu)", "scale": 100}`), source table + filter, applicable entity types; plus `non_averageable_columns` and `non_summable_columns` lists used by the SQL guard | — |
| `capability_manifest.json` | topics with `status` = `loaded` / `not_loaded` / `partial`, their core/mart tables, trigger **keywords**, and the exact **note** the bot gives | a topic marked `loaded` is empty, or `not_loaded` has rows |
| `known_issues.json` | `KI-nn`: severity, status (open/accepted/fixed/on_hold), title, detail, the one-line **caveat** attached to affected answers, `affects` list | — |
| `example_queries.sql` (in the app's semantic folder) | numbered, reviewed question → SQL blocks (`-- 12. <question>` … `;`) used as prompt few-shots AND as the SQL of verified answers; every block executed by tests | — |
| `knowledge_base.yaml` (optional) | curated non-data knowledge (organisation, policies, definitions), each entry with aliases, facets and `sources` (kept for maintainers, never shown) | — |

### 4.2 Warehouse tables

```sql
semantic_table_doc   (table_name PK, table_group, grain, doc, row_count, date_from, date_to)
semantic_column_doc  (table_name, column_name, data_type, unit, additive, doc, PK(table_name, column_name))
semantic_metric      (metric_key PK, label, unit, definition JSON)
semantic_capability  (topic PK, status, mart_tables, core_tables, row_count, note)
semantic_data_period (key PK, value)   -- data_from, data_to, data_as_of, latest_full_period, partial_period, data_nature
```
Row counts and date ranges are **measured at build time**, never typed by hand.

### 4.3 Exported `semantic_layer.json` (what the app reads)

```jsonc
{
  "database": "...", "data_period": {"data_from": "...", "data_to": "...", "data_as_of": "...", ...},
  "rules": ["Query only mart_* tables ...", "Ratios are SUM(num)/SUM(den) ...", ...],
  "glossary": {"PLF": {"aliases": [...], "plain": "..."}},
  "metrics": [{"key": "plf", "label": "...", "unit": "%", "synonyms": [...], "expr": {...}}],
  "non_averageable_columns": [...], "non_summable_columns": [...],
  "tables": {"mart_fact_x": {"group": "generation", "grain": "...", "doc": "...", "row_count": 0,
             "date_from": "...", "date_to": "...",
             "columns": {"col": {"type": "REAL", "unit": "MU", "additive": true, "doc": "..."}}}},
  "capabilities": [{"topic": "billing", "status": "not_loaded", "mart_tables": [], "keywords": [...], "note": "..."}],
  "caveats": [...],
  "entities": {"stations": [...], "units": [...], "reservoirs": [...], "reasons": [...], "suppliers": [...],
               "fys": [...], "water_years": [...]}
}
```
The exporter also writes **`integrity_manifest.json`** (SHA-256 of the DB, the JSON and the example library); a test
fails if they change unexpectedly. Rebuild order: `build_warehouse.py` → `build_semantic_layer.py` → restart the app.

**Contract:** everything the AI needs to know about the data, in one file, generated from validated metadata.

---

## 5. L7 — Ledger and build checks (`ledger_*`)

| Table | Contents |
|---|---|
| `ledger_file` | copy of the file registry |
| `ledger_column` | **every raw column** → `stg_type` → `landed_in` (core column, `UNMAPPED (<reason>)`, or `NOT LOADED (<reason>)`) |
| `ledger_issue` | the known issues |
| `ledger_reconcile_log`, `ledger_reconciliation` | reconcile actions and anchor checks |
| `ledger_build` | run timestamp, stage, seconds |
| `ledger_build_check` | named PASS/FAIL invariants |

Write **build checks** that compare the mart against independent totals, for example:
- mart FY/month totals = the source's own summary sheets (every entity-period);
- daily fact row count = raw row count; unit-level total = station-level total;
- physical invariants (hours per day sum to 24; no negatives; aux ≤ gross);
- 2–3 **golden figures** from the trusted report;
- reconciliation all PASS; every mart column documented.

Any FAIL aborts the build.

---

## 6. L8 — Database access layer (how the app connects)

One module (`db.py`) owns the only connection. Three independent protections:

1. **Read-only at the engine.** SQLite: `sqlite3.connect("file:<path>?mode=ro", uri=True)` + `PRAGMA query_only = ON`.
   (Postgres: a role with `SELECT` only on the mart schema and `default_transaction_read_only = on` — §16.)
2. **Compile-time authorizer for model-written SQL** (`restricted=True`): allow only `SELECT`, `READ`, `FUNCTION` actions;
   `READ` only on tables starting `mart_` (plus harmless table functions like `json_each`); deny file/extension functions
   (`load_extension`, `readfile`, `writefile`, …). This holds however the SQL is phrased (aliases, CTEs, sub-queries).
3. **Wall-clock timeout** (interrupt the statement) and **row cap** (`fetchmany(max_rows + 1)` → `truncated` flag).

```python
def _restricted_authorizer(action, arg1, arg2, _db, _src):
    if action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}:
        return sqlite3.SQLITE_DENY                       # writes, PRAGMA, ATTACH, recursive CTE, transactions
    if action == sqlite3.SQLITE_READ and not (arg1 or "").lower().startswith("mart_"):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
```

- Opened once in the app's lifespan; shared through a **single-worker executor + lock** (SQLite connections aren't
  thread-safe); async callers `await db.run(sql, max_rows, timeout, restricted, params)`.
- **Reviewed queries (dashboards, reports) also run `restricted=True`** and receive request values only as **bound
  parameters**, after checking them against lists the DB itself returns (unknown FY/station → 404, never a query).
- Result object: `columns, rows, truncated, elapsed_seconds`. Authorizer denials raise a distinct `RestrictedQueryError`
  so the repair prompt can say "read only the mart tables".

---

## 7. L9 — AI layer: the question pipeline

One function, shared by the streaming endpoint, the JSON endpoint and the evaluation scripts. **Order matters**:

```
 0  progress("understanding")
 1  guardrails.screen_question        hard refusals: empty / too long / pasted code / injection / harmful /
                                      write request / personal data                         → fixed refusal
 2  identity                          "who are you"                                        → fixed answer
 3  verified queries                  near-exact match to a reviewed question              → reviewed SQL + writer
 4  definitions                       "what is X?" from the knowledge base, then the data glossary
 5  knowledge vs capability           general org/policy questions → knowledge base;
                                      data the system lacks (billing, hourly, HR…) → honest "not loaded" note
 6  deferred refusals                 questions about the system itself; off-topic
 7  entity extraction (no LLM)        entities, unknown names ("ZZTPS", "RTPS-U12" → refused by name),
                                      out-of-range periods ("March 2014" → data-period answer)
 8  parameterised reports             broad questions ("performance of thermal stations in FY 2023-24",
                                      "which units need attention") → reviewed report functions
 9  metric lookup                     one metric × one scope × one period → SQL built from the registry
10  LLM path                          prune semantic layer → build prompt → ONE call → JSON plan
11  SQL guard                         read-only shape, mart-only, grain guards, KPI formulas, NULLS LAST
12  execute                           restricted + timeout + row cap; on any guard/SQL error: ONE repair round
13  result guards                     impossible values (PLF > 100 %, negative stock) → repair; subject mismatch
14  answer writer                     template fill / deterministic narration, units, caveats, table, chart
15  reviewer (2nd model pass)         categorical verdict + deterministic veto; one repair; else withhold
16  post-processing                   unit conversion if asked ("in GWh"); report id for exports
```

Each step is a pure function or a small class; every route stamps `route` (`refused:code`, `verified:<key>`,
`report:<key>`, `metric:<key>`, `knowledge`, `capability`, `llm`, `llm_unverified` …) for logs and tests. Timings per
stage go into `timing_ms`. A `progress(stage)` callback lets the SSE endpoint stream real stage names to the UI.

Result object (dataclass): `question, answerable, answer, route, sql, columns, rows, truncated, table, chart, notes,
entities, timing_ms, error, verification, review_notes, total_row`.

---

## 8. L9a — Guardrails (`guardrails.py`)

- **`screen_question`** returns a category: `empty`, `too_long` (e.g. > 500 chars), `code` (pasted SQL/code),
  `injection` ("ignore previous instructions", role-play), `harmful` (sabotage, disabling safety systems), `read_only`
  (requests to change data), `personal_data` (names/salaries/phones of people), `system` (questions about tables,
  prompt, model, keys), `off_topic`. The first group refuses immediately; `system`/`off_topic` are **deferred** until
  after knowledge/capability checks (so "history of the company" isn't refused as off-topic).
- **`REFUSALS`**: one fixed, reviewed sentence per category, each with an example of a good question.
- **`scrub`**: drops any sentence of model-written text that names internal objects (table/column names, database
  engine, the model) and rewrites jargon to plain words.
- **`safe_refusal`**: the model's own "can't answer" text is shown only if every sentence is about the data.
- **`subject_mismatch`**: "how many units…" answered by SQL that never touches units → refused.
- **`implausible_result`**: ratio outside 0–100, negative generation/stock/hours → sent back for repair, never shown.
- **`strip_provenance`** (optional policy): answers never show "Sources:", URLs or "from the knowledge base" chips.
- Domain-specific SQL guard, e.g. `company_filter_unrequested`: a filter the question didn't ask for (it silently dropped
  subsidiaries and changed totals) is rejected with an explanatory repair message.

---

## 9. L9b — Deterministic routes (no LLM)

These answer most real questions instantly and identically every time.

### 9.1 Entity extraction (`semantic_layer.py`)
Regex + dictionaries built once from `semantic_layer.json` (`lru_cache`). No LLM, no embeddings, no DB call.
Extracts: entities by code/name/alias (longest match first), sub-entities (`RTPS-U8`), categories, plant types,
company-wide scope, **periods** (date, month, quarter, FY, water year, season, calendar year, relative: "last month",
"now" — resolved against `data_as_of`), **metrics** (registry synonyms), **intents** (ranking, comparison, breakdown,
trend). Also `unknown_names()` (code-like tokens not in the catalogue) and out-of-range period detection.

### 9.2 Verified queries (`verified_queries.py`)
Reviewed SQL (the numbered blocks of `example_queries.sql`) + a hand-written renderer per block. Matching is
deliberately conservative: normalised question very close to a registered phrasing (difflib ratio) **AND** identical
entity tokens (codes, years, months) **AND** same direction (highest vs lowest). "BTPS" never matches the RTPS entry.

### 9.3 Metric lookup (`metric_lookup.py`)
One metric × at most one scope × exactly one period, no ranking/trend wording → SQL **assembled from the registry
expression + fixed filters**, with only **bound parameters** from validated entities. Picks the right table per scope and
period (unit-daily for a day, station-monthly for a month, station-FY for an FY). Adds "pinned to the official report"
when the row's `anchor_scope` says so. Levels over a period return closing value + low/high/average, not a sum.

### 9.4 Parameterised reports (`reports.py`, `breakdown.py`)
Functions `report(question, entities, db, timeout) -> Report | None` tried in order: period overview, entity status,
lists, at-risk items, "needs attention", stock situation, scorecards, trends, extremes, efficiency, breakdowns by
day/week/month/quarter/year/entity, FY summary. Rules:
- a report is skipped if it doesn't cover a measure the question names (`covers_asked_measures`);
- if no period was named, a default is used **and stated** ("No period was named, so this uses FY 2024-25");
- breakdowns come from the **same functions as the dashboard**, and return an independent total row for checking.

### 9.5 Capability notes (`capability.py`)
Keyword match against `capabilities[*].keywords` where `status != loaded` → the manifest's note. The system never
pretends to have data it doesn't.

### 9.6 Knowledge base (`knowledge.py`, optional)
Curated YAML for non-data questions (what the organisation is, regulators, policies, definitions). Longest-alias match
across groups. Questions that name a period, measure or count always go to the data instead. Also used as the fallback
when the LLM is unreachable or declines.

---

## 10. L9c — The LLM path

### 10.1 Prompt (`prompts.py`) — built per question, within a token budget
1. **Fixed instructions**: domain one-liner, dialect, output contract, hard rules (mart only; SUM/SUM; one fact grain per
   query, UNION ALL for side-by-side grains; exact codes/labels; "now" = data end; rankings exclude NULLs; answerable=false
   for data not listed — never general knowledge).
2. **Schema of only the relevant mart tables** (`prune_semantic_layer`: table *groups* chosen by keywords, metrics and
   entities) with grain, unit, column docs.
3. **Formulas of the metrics named** (+ 1–2 headline metrics), always-on rules, glossary terms that appear.
4. **Top-k worked examples** by word overlap with the question (k = 8, reduced under budget pressure).
5. **Verified entities** as JSON (codes, periods, data end).
Budget fallback order: drop examples (8→4), shorten column docs, drop examples entirely. Estimate tokens as chars/4.

### 10.2 Output contract (JSON, one object)
```json
{
  "answerable": true,
  "sql": "SELECT ... AS total_gen_mu FROM mart_fact_station_generation_fy WHERE fy = '2023-24' AND station_key = 'RTPS'",
  "answer_template": "RTPS generated {total_gen_mu} in FY 2023-24.",
  "clarification_message": "",
  "chart_data": {"type": "bar", "label_column": "station", "value_column": "total_gen_mu", "series_label": "Gross generation (MU)"}
}
```
- Every selected column aliased; literal values baked in; **no `{placeholder}` in SQL** (guard rejects it).
- `answer_template` uses only aliases or entity keys; the filler appends units from the alias suffix.
- Parse defensively: strip `</think>` preambles and code fences, extract the first `{…}`, fill missing keys, repair or drop
  a malformed chart spec. If the reply was cut off (`finish_reason == "length"`), retry once with a "select only the few
  columns needed" note and a slightly different temperature (0.0 → 0.2).
- Call: OpenAI-compatible `/chat/completions`, `temperature 0`, bounded `max_tokens`, reasoning disabled if supported.

### 10.3 SQL guard (`sql_safety.py`) — fail closed, messages written for the repair turn
Shape: strip comments; mask strings; exactly one statement; starts with `SELECT`/`WITH`; no write/admin keyword, no
`RECURSIVE`; every `FROM/JOIN` target is `mart_*`, a CTE name or an allowed table function.
**Grain guards** (the silent-wrong-number killers):
- `AVG(col)` where col ∈ `non_averageable_columns`, `SUM(col)` where col ∈ `non_summable_columns` → reject with the
  SUM/SUM formula;
- two generation facts of different grain in one query without `UNION` → reject (double counting);
- a lookup holding two code families joined without the family filter → reject;
- point-in-time table used for a past period → reject.
**KPI formula guard**: a column aliased like a KPI (`plf_pct`, `aux_pct`…) must use that KPI's numerator/denominator
columns.
**Rewrite**: every ascending `ORDER BY` term gets `NULLS LAST` (so "lowest" never names a row with no figure).
Return the cleaned SQL; execute *that*, not the model's text.

### 10.4 Execute and repair
`db.run(safe_sql, restricted=True)`; on `UnsafeSQLError`, `RestrictedQueryError`, SQL error or timeout → **one** repair
call with the exact error appended to the original prompt; same checks again; still failing → fixed "can't answer
reliably" refusal (never a raw error). A false "that period is outside the data" decline is corrected once with a
coverage note.

### 10.5 Answer writer (`answer.py`) — deterministic
- `fill_template`: fill `{alias}` from the **first row**; format numbers by column suffix (MU, %, kcal/kWh, tonnes),
  dedupe units, repair a template that repeats one slot.
- Multi-row results: deterministic narration — leader, share of total (additive measures only), spread, first-to-last
  trend; shortfalls stored negative narrated by size.
- `caveats`: known-issue caveats for tables/columns used; "only first N rows shown" when truncated.
- `result_table` and `build_chart` (bar/line; doughnut only for additive quantities, never for %).
- `summable_over_time(col)`: stock/level/storage columns never get a total row.
- `convert_units`: deterministic conversion appended when asked ("in GWh", "in lakh tonnes"); original figure kept.

### 10.6 Reviewer — second model pass with a deterministic veto (`review.py`)
Free-form LLM reviewers invent objections (in testing: 23 false rejections out of 213 correct answers). So:
1. Give it the question, entities, SQL, docs of the columns the SQL uses, data rules, result rows (≤ 25), answer text.
2. It must reply `{"ok": bool, "category": one of wrong_measure | wrong_scope | wrong_period | wrong_aggregation |
   unsupported_claim, "problem": "..."}`; style, rounding and wording are never errors.
3. **Veto** a rejection when code can prove it false: scope → SQL filters on every entity named; period → SQL filters on
   every period named; measure → SQL uses the registry formula's columns; aggregation → result shape matches the
   question; unsupported_claim → every figure in the answer comes from the rows; any objection demanding a forbidden
   filter.
4. A surviving rejection → one repair round with the objection → reviewed again → still rejected → **withhold** (no
   figure, no table, no chart; "couldn't confirm" message).
5. Reviewer unreachable → **fail open** (the answer already passed every deterministic guard) and log it.
Deterministic routes (verified, metric, reports) are reviewed code and skip this pass.

---

## 11. L10 — Service / API layer

| Method | Path | Notes |
|---|---|---|
| GET | `/`, `/chat`, other pages, `/health` | static pages; health returns data period + whether the LLM is configured |
| GET | `/api/dashboard?period=&entity=` | fixed reviewed queries; unknown values → 404 |
| POST | `/api/ask` | **SSE**: `status{stage}` (one per real pipeline stage) → `sql` (admins only) → `answer_chunk…` (paced word groups) → `chart` → `table` → `done{answerable, truncated, report_id, …}` |
| POST | `/api/ask_once` | same pipeline, plain JSON (scripts, evaluation) |
| GET | `/api/<report>?…`, `/api/answer-report/{id}` | optional exports (§13) |

Cross-cutting:
- **Settings** via `pydantic-settings` from `.env` (gateway URL/model/key, CA bundle, timeouts, row cap, token budget,
  rate limit, concurrency, request-size cap, reviewer on/off, `SHOW_TECHNICAL_DETAILS`). Without LLM settings the app
  still runs every deterministic route.
- **Protection middleware**: per-IP rate limit (e.g. 60/min on `/api/*`), request-body cap (16 KB → 413), security
  headers (`nosniff`, `DENY` framing, no referrer, permissions-policy, COOP, strict **CSP: no inline scripts**).
- **Concurrency**: a semaphore (e.g. 8 questions at once, wait ≤ 20 s, then a polite "busy").
- Validation errors and unhandled exceptions return fixed user-safe messages; details only with technical mode on.
- SQL, routes, entities, timings shown only when `SHOW_TECHNICAL_DETAILS=true`; `/docs` disabled otherwise.
- Single process assumption: in-memory caches (kept reports, rate limiter) need Redis or similar for multi-worker.

---

## 12. L11 — Front end

Plain HTML + separate `.js` files (CSP forbids inline scripts); Chart.js for charts (vendor it locally for offline
networks); CSS custom properties for theming.

- **Chat page**: welcome screen with suggested questions; streamed status steps ("Understanding → Querying → Checking
  the SQL → Writing the answer"); answer typed out from `answer_chunk`s; chart; collapsible data table with CSV download
  (escape formula-like cells); copy button; technical details only in admin mode; once-per-session splash; language
  toggle; drawer sidebar on narrow screens; reduced-motion support.
- **Dashboard**: KPIs with period-on-period chips, trend charts with granularity toggle (monthly/weekly/daily), entity
  league table with drill-down (`?entity=` bookmarkable), "as on" panels for point-in-time data. Built from **the same
  reviewed queries** the chat's reports use.
- **Report viewer** (optional): renders the exact export workbook on screen before download.
- Tone: for official users keep it formal and restrained; never show provenance chips if policy says so.

---

## 13. L12 — Reports and exports (optional)

- Export builders read **the same data functions as the dashboard**, so an export can't disagree with the screen.
- Every workbook passes a `finalize()` step: text beginning `= + - @` stored as text (no live formulas / CSV injection),
  draft footer + document properties, **integrity checks travel with the file** (parts add up to the whole; stated
  figure = data total). A failing check → HTTP 409 "under review", never a wrong file.
- Chat answers that came from data get a server-side `report_id` (random, TTL e.g. 3 h, max N kept, bound to the
  warehouse **data version** = hash of the DB file); the export is built from what the server kept, never from what the
  page sends back.

---

## 14. Testing and evaluation

| Suite | What it proves |
|---|---|
| DB guard | writes, PRAGMA, ATTACH and non-mart reads are refused **at the engine** |
| SQL safety | every guard rule, incl. grain and KPI-formula cases, and the NULLS LAST rewrite |
| Router / entities | codes, aliases, periods (incl. relative), unknown names, out-of-range |
| Golden figures | 10–20 figures from the trusted report answered **without the LLM** |
| Verified + examples | every example block runs through the guard and authorizer and returns rows; every verified renderer works |
| Pipeline (mocked LLM) | every route, repair round, unsafe SQL, offline mode, reviewer approve/reject/repair/veto/unreachable |
| Streaming | stages arrive in order; chunks join to exactly the `/ask_once` answer |
| Dashboard/reports | totals add up; unknown/hostile params → 404; export checks; formula escaping |
| Integrity | SHA-256 manifest of the DB, semantic layer and examples unchanged |
| Knowledge | no provenance in answers; entries consistent with the warehouse |

Plus two live harnesses (need the model gateway):
- **Stress bank** (`run_stress.py`): ~200 questions across every entity, period type, measure, unit conversion, typo and
  edge case, 30+ must-refuse and 10+ must-allow guardrail cases. **Expected figures are recomputed by independent SQL
  written separately from the bot's** (e.g. from the most granular fact, not the roll-up the bot uses) and must appear in
  the answer. Writes a JSON report and a Markdown table.
- **Eval** (`run_eval.py`): ~15 questions phrased unlike the example library, each checked against reference SQL.
Rule of thumb from this project: first stress run 135/182; fixing each failure *at its cause* (not by special-casing the
question) and adding a test reached 213/213.

Tests set the rate limit high in `conftest.py`; the limit has its own test.

---

## 15. Adoption plan for an existing NL2SQL bot

Do these in order; each step is independently valuable.

1. **Lock the database** (§6): read-only role/file, compile-time allow-list of curated tables, timeout, row cap. Add the
   DB-guard tests. *Biggest safety win, smallest change.*
2. **Carve out a mart** (§3 L5): wide, one-grain-per-table, denormalised dates/entities, helper numerator/denominator
   columns, "now" = data end. Point the bot only at it.
3. **Write the metric registry and column docs** (§4): every KPI with num/den and synonyms; non-averageable and
   non-summable lists; unit-by-suffix. Generate `semantic_layer.json`; make the build fail on undocumented columns.
4. **Add the SQL guard** (§10.3) with grain + KPI-formula rules and one repair round.
5. **Switch the model to the template contract** (§10.2): SQL + `answer_template`; Python fills numbers. Remove any step
   where the model sees rows and writes figures.
6. **Add deterministic routes** (§9): entity extraction → metric lookup → verified queries → the 5–10 broad reports your
   users ask most → capability notes.
7. **Add guardrails** (§8) with fixed refusal wording.
8. **Add the reviewer with veto** (§10.6) only after 1–7; measure false rejections on your stress bank.
9. **Build the stress bank with independent expected SQL** (§14) and iterate to 100 %.
10. **Backfill the lower warehouse layers** (raw → staging → core → ledger, reconcile if you have trusted figures) so every
    number is traceable and the build self-checks.

Gap-assessment questions for your current bot:
☐ Can the model's SQL read anything but curated tables? ☐ Can it write? ☐ Does it ever average a ratio?
☐ Does the model ever type a figure the user sees? ☐ Is there one documented formula per KPI?
☐ Does "not in this data" have an honest answer? ☐ Are golden figures tested without the LLM?
☐ Is there a stress bank with independently computed expected values? ☐ Do dashboard and chat share queries?

---

## 16. Postgres / other-engine equivalents

| This design (SQLite) | Postgres |
|---|---|
| table prefixes `raw_`, `stg_`, `core_`, `mart_`… | schemas `raw`, `stg`, `core`, `mart`, `semantic`, `ledger` |
| `mode=ro` + `PRAGMA query_only` | dedicated role: `GRANT USAGE ON SCHEMA mart; GRANT SELECT ON ALL TABLES IN SCHEMA mart`; `ALTER ROLE … SET default_transaction_read_only = on` |
| `set_authorizer` (mart only) | the role has no privileges outside `mart`; revoke `PUBLIC` on other schemas; optionally parse with `sqlglot` and allow-list tables |
| `conn.interrupt()` timeout | `SET statement_timeout = '30s'` per session/role |
| `fetchmany(max+1)` | same, or wrap `SELECT * FROM (<sql>) q LIMIT max+1` |
| physical mart tables | tables or **materialised views** refreshed by the build; never plain views over huge facts |
| `PRAGMA foreign_key_check` | real FK constraints in `core` (validated on load) |
| `json_each` allow-list | allow-list of safe functions; deny `pg_read_file`, `dblink`, `COPY`, `lo_*` |
| multi-tenant | Row-Level Security on mart tables keyed by a session GUC |

Regex SQL guards work on any dialect; for robustness parse with an AST library (e.g. `sqlglot`) where available.

---

## 17. Appendix

### 17.1 Folder layout (reference)
```
project/
├─ <source files>                      inputs, never modified (+ anchors.json of trusted figures)
├─ warehouse/
│  ├─ build_warehouse.py               staged builder (sources → … → checks)
│  ├─ reconcile.py                     anchor reconciliation + independent verify
│  ├─ schema_core.sql                  reviewed core DDL
│  ├─ mart.sql                         mart DDL (CREATE TABLE … AS), {{DATA_AS_OF}} placeholder
│  ├─ metadata/                        semantic_docs.json · metric_registry.json · capability_manifest.json ·
│  │                                   known_issues.json · _lineage.json (written by the build)
│  └─ <db file>
└─ webapp/
   ├─ .env / .env.example              gateway + app settings (.env is secret)
   ├─ integrity_manifest.json
   ├─ semantic_layer/                  build_semantic_layer.py · semantic_layer.json (generated) ·
   │                                   example_queries.sql · knowledge_base.yaml
   └─ app/
      ├─ main.py          routes, SSE, lifespan       ├─ pipeline.py      the ordered pipeline
      ├─ config.py        settings                    ├─ guardrails.py    screening, refusals, scrub
      ├─ protection.py    rate limit, CSP, body cap   ├─ semantic_layer.py entity extraction, pruning
      ├─ db.py            read-only DB + authorizer   ├─ sql_safety.py    SQL guard
      ├─ llm.py           the model call(s)           ├─ prompts.py       prompt + repair prompts
      ├─ review.py        reviewer + veto             ├─ answer.py        deterministic writer
      ├─ verified_queries.py · metric_lookup.py · reports.py · capability.py · knowledge.py
      ├─ dashboard_data.py  reviewed queries shared by dashboard, reports, exports
      ├─ static/          *.html · *.js · *.css (no inline scripts)
      └─ tests/           pytest suites + run_stress.py + run_eval.py
```

### 17.2 Metric registry entry
```json
{
  "key": "plf", "label": "plant load factor (PLF)", "unit": "%", "precision": 2, "kind": "ratio",
  "synonyms": ["plf", "plant load factor", "load factor", "capacity utilisation factor"],
  "expr": {"all": {"num": "SUM(gross_gen_mu)", "den": "SUM(capacity_mu)", "scale": 100}},
  "source_table": null, "filter": null, "applies_to": ["unit", "station", "category", "company"]
}
```
`expr` may have per-family variants (`daily`, `rollup`) when column names differ between grains.

### 17.3 Capability topic
```json
{"topic": "billing", "status": "not_loaded", "core_tables": ["core_energy_bill"], "mart_tables": [],
 "keywords": ["bill", "invoice", "tariff", "revenue"],
 "note": "Billing and sale-of-energy data are not loaded in this system, so I can't give bill amounts."}
```

### 17.4 Known issue
```json
{"id": "KI-02", "severity": "medium", "status": "on_hold",
 "title": "Category split differs from the official dashboard",
 "detail": "…", "caveat": "Category totals follow the source workbook's grouping, which differs from the official one.",
 "affects": ["mart_dim_station.plant_category_label"]}
```

### 17.5 Pipeline skeleton
```python
async def answer_question(q, db, max_rows, timeout, progress=noop) -> Result:
    r = Result(q); progress("understanding")
    if (cat := guardrails.screen_question(q)) in HARD_REFUSALS:   return r.finish(REFUSALS[cat], f"refused:{cat}", False)
    if is_identity(q):                                              return r.finish(IDENTITY, "identity")
    if vq := verified_queries.match(q):                             return await run_verified(r, vq, db)
    if kb := knowledge.definition(q):                               return r.finish(kb, "knowledge")
    if note := capability.check(q):                                 return r.finish(note, "capability", False)
    if cat in ("system", "off_topic"):                              return r.finish(REFUSALS[cat], f"refused:{cat}", False)
    ents = extract_entities(q)
    if bad := unknown_names(q):                                     return r.finish(unknown_msg(bad), "unknown_name", False)
    if out_of_range(ents):                                          return r.finish(period_msg(ents), "out_of_range", False)
    progress("querying")
    if rep := await reports.match_and_run(q, ents, db, timeout):    return r.from_report(rep)
    if ma := await metric_lookup.answer(ents, db, timeout, q):      return r.from_metric(ma)
    return await llm_path(r, q, ents, db, max_rows, timeout, progress)   # §10: plan → guard → run → write → review
```

### 17.6 Definition of done for a new dataset
☐ build runs end to end and every check passes ☐ every mart column documented ☐ every KPI in the registry with num/den
☐ capability manifest matches row counts ☐ golden figures reproduce without the LLM ☐ DB-guard tests pass
☐ example library runs through guard + authorizer ☐ stress bank 100 % with independent expected SQL
☐ integrity manifest written ☐ dashboard and chat agree on every shared figure.
