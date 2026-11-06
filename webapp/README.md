# KPCL GMS Assistant — NL → SQL → NL chatbot

FastAPI chatbot over the SQLite warehouse (`../warehouse/kpcl_gms.db`). Architecture: the WB POC's protected, verified layers, plus the Cescom POC's grain guard, offline tests and locked-inputs rule, plus KPCL-specific parts (metric registry, capability manifest, data-as-of date, deterministic metric lookup). **One LLM call at most per question**; everything before and after it is deterministic.

## Run

```
cd webapp/kpcl-ai-app
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000        # http://localhost:8000 = dashboard, /chat = assistant
```

`webapp/.env` holds the internal vLLM gateway settings (same variables and values as the WB POC; the VPN must be connected for the LLM path). Without them the app still runs in offline mode: verified reports, single-figure lookups, definitions and capability answers work; other questions get a "model not reachable" message. Set `KPCL_SHOW_TECHNICAL_DETAILS=true` to see SQL, route and timings in the UI.

## Request flow (`app/pipeline.py`)

```
question
 L1  guardrails.screen_question   empty / >500 chars / pasted SQL or code / prompt injection      -> fixed refusal
 L2a identity                     "who are you"                                                    -> fixed answer
 L2b verified_queries             reviewed SQL + prose writer (33 report questions)               -> answer, chart, table
 L2c glossary                     "what is PAF?"                                                   -> plain definition
 L2d capability                   JMR / billing / defects / hourly / KERC / HR / holidays         -> honest "not loaded" note
 L2e knowledge.primary            "what is KPCL?", "when was it founded?", "what is KERC?",       -> knowledge_base.yaml
                                  "tell me about RTPS" (no period, measure or unit named)            (also the fallback when
                                                                                                     the model declines or is
                                                                                                     unreachable)
 L1' screen (deferred)            questions about the system; off-topic / general knowledge       -> fixed refusal
 L3  semantic_layer router        stations, units, categories, reservoirs, periods (incl. "last month" against
                                  30-Apr-2025), metrics, intents — regex + dictionaries, no LLM
     unknown_names                "ZZTPS", "RTPS-U12", "a plant called X"                          -> refused by name
     out-of-range period          "March 2014"                                                     -> data-period answer
 L4  metric_lookup                one metric × one scope × one period, SQL from the registry      -> answer (+ "pinned to
                                                                                                     the K-eRP report")
 L5  prompts.build_prompt         relevant mart tables only, formulas, rules, 8 most similar worked examples, verified
                                  entities, within a 20k-token budget
 L6  llm.generate_query_plan      ONE call → {answerable, sql, answer_template, clarification, chart}; defensive parse
 L7  sql_safety                   one SELECT/WITH · no {placeholder} · mart_* only · no AVG/SUM of ratios or levels ·
                                  no join of two generation grains · reason family filter · status table not for past
                                  periods · KPI-named columns must use their formula · NULLS LAST on ascending sorts
 L8  db.ReadOnlyDB                mode=ro + query_only + authorizer (mart_* reads only, no writes / PRAGMA / ATTACH /
                                  recursive CTE / file functions) + timeout + 200-row cap
     on any L7/L8 failure         ONE repair call with the exact error, then the same checks again
 L9  result guards                subject mismatch ("how many units" answered without units), empty result
 L10 answer writer                template fill with units (MU, %, kcal/kWh, t …), or narration: leader, share of total
                                  (additive measures only), spread, first-to-last trend; long-shutdown note; caveats
                                  from known issues; data table; bar/line chart
 SSE  status (one per real stage, as it starts) → sql (admins) → answer_chunk… (paced) → chart → table → done
```

**Streaming.** `answer_question(..., progress=cb)` calls `cb(stage)` as each real stage starts:
`understanding`, `querying`, `writing_sql`, `checking_sql`, `running_query`, `repairing`, `writing_answer`.
`/api/ask` forwards these as `status` events while the pipeline runs, then streams the answer as word chunks, grouped so a long answer finishes in about two seconds. No pause is shorter than 25 ms, because Windows timers tick every ~15 ms. The chart and table follow the text. `tests/test_streaming.py` checks that the stages arrive in order and that the chunks join to exactly the `/api/ask_once` answer.

## Frontend (`app/static/`)

`chat.html` is one self-contained page: the WB and Cescom assistants' shell, restyled in a formal, government-briefing look.
* **Palette:**
  * KPCL portal navy `#072577` as the primary colour, with a muted gold accent.
  * Ivory paper surfaces, and serif headings (Noto Serif / Noto Serif Kannada).
  * The Karnataka flag's yellow over red as a hairline under the header.
  * Red is used only for errors.
* **Header:**
  * The emblem and KPCL mark with the organisation's name, and an English / ಕನ್ನಡ switch.
  * A navy bar reading "Generation Management System · Thermal · Hydro · Solar · Wind · April 2015 – April 2025", with the AI MODE badge at the far right.
  * The model status is a small muted dot in the footnote.
* **Sidebar:** "New conversation", then the 11 suggested questions grouped under Performance, Operations, Fuel and water, and Reference.
* **Welcome screen:** a stat strip and six question cards.
* **Answers:**
  * Live stage steps while the answer is being worked on, then the text typing out with a cursor.
  * Chart.js charts (bar or line; doughnut only for additive quantities, never for %, factors or rates), with an SVG fallback when the CDN is unreachable.
  * A collapsible data table with CSV download, and a copy button.
  * Technical details only when `KPCL_SHOW_TECHNICAL_DETAILS=true`.
* **Other behaviour:**
  * A once-per-session splash.
  * The chosen language is remembered in the browser.
  * The page honours reduced motion.
  * A drawer sidebar below 1024 px.

Assets:
* `karnataka-emblem.png` is the state emblem from kpcl.karnataka.gov.in.
* `kpcl-mark.svg` is a simplified vector redraw of the KPCL corporate mark. The only public copy is 77×95 px; swap in an official file if KPCL supplies one.
* `kpcl-skyline.svg` is the backdrop: dam, cooling towers, transmission line, wind turbines and solar park.

## Dashboard (`app/dashboard_data.py`, `app/static/dashboard.html`)

`/` (and `/dashboard`) is the entry page, as in the Cescom POC; `/chat` is the assistant. `GET /api/dashboard?fy=2024-25&station=RTPS` (both optional) returns one JSON summary built from fixed, reviewed, parameterised queries. They run through the same mart-only authorizer as model SQL and use the registry formulas, so the dashboard and the chatbot always agree. The FY and station are checked against the database's own lists first; anything else is a 404. The default period is the latest complete FY (2024-25). "As on" panels use the last day in the data (30-Apr-2025).

Built for a senior reader who wants the answer at a glance:
1. **Key takeaways:** plain-English sentences written from the data (generation and change on last year, thermal PLF, CEA target shortfalls, units out, coal below norm, lowest reservoir, share of state consumption), each marked good, bad or info.
2. **Headline generation** with a count-up figure and a ten-year sparkline, then five KPI cards with year-on-year chips:
   * Company view: thermal PLF, CEA achievement ring, hydro generation, units out, share of Karnataka's consumption.
   * Station view: PLF, CEA achievement, aux %, units out, trips.
3. **Monthly generation** against last year and the CEA target, and the **generation mix** donut.
4. **Station table:** capacity, generation, change on last year, PLF and a target-achievement bar. It is sortable and filterable by category. Clicking a row, or a station in the sidebar, opens that station's dashboard with its units table; the URL `?station=` is bookmarkable and works with the browser's back button.
5. **As on 30-Apr-2025:** units out of service (days out, reason), coal stock against the 20-day norm with a monthly trend, and reservoir levels as filling tanks. A station view shows only its own coal or reservoirs.
6. **Losses and ten-year view:** generation lost by reason, and a ten-year view (generation with CEA achievement, or with station PLF).

**Generation panel toggle (Monthly / Weekly / Daily)** for the FY and scope in view (all stations, or the station open), for every FY. Monthly shows the CEA target; weekly (Monday-Sunday calendar weeks from the workbook calendar) and daily come from the daily generation sheet and are compared with the same weeks / dates a year earlier (CEA targets exist only monthly, so none is invented). Daily and weekly totals are tested to add up exactly to the FY total. The choice is remembered across FYs and stations.

In the weekly and daily views, the chart is shaded in alternating bands, one per month, with a month label above each band. A week belongs to the month its middle day falls in. Below the weekly chart, a **Week by week** strip shows one card per month with that month's exact total. Each card lists its weeks with the date range, a partial-week tag, MU, a proportional bar and the change on the same week a year earlier. Pointing at a week highlights its bar in the chart, and clicking a bar scrolls the strip to its month. The strip scrolls sideways (with ‹ › buttons) and becomes a one-card swipe on phones.

**Insights** (`/insights`, `app/static/insights.html`, `GET /api/insights?fy=`) is its own page, opened from the sidebar with the same logo transition: 17 key insights for the FY as KPI boxes (figure, change on last year, status colour, one-line explanation, trend line where there is one), grouped into Performance, Targets, Reliability, Fuel and water, Efficiency and grid, with filters for Needs attention / Going well / For information. It is built from the same reviewed queries as the dashboard (`build_insights` in `app/dashboard_data.py`), so the figures always match. Both pages share `app/static/dashboard.css`.

"Ask AI about this" and the AI MODE badge play Cescom's "Entering AI Mode" transition into the chat; the chat's sidebar links back.

## Semantic layer (`kpcl_semantic_layer/`)

* `semantic_layer.json` — generated by `build_semantic_layer.py` from the warehouse's `semantic_*` tables and `warehouse/metadata/*.json`: every mart table and column with grain, unit, additivity and doc; 28 metrics; 18 capability topics; 24 glossary terms; rules; entity lists and aliases; caveats. Never edit by hand.
* `example_queries.sql` — 36 reviewed question → SQL pairs (the original 24-question bank rewritten for SQLite, plus 12). Used as prompt examples and as the SQL of the verified answers; every block is executed by the tests.
* `../integrity_manifest.json` — SHA-256 of the database, the layer and the examples, written by the builder; `tests/test_integrity.py` proves the app never changed them.
* `knowledge_base.yaml` is the general knowledge base, modelled on the WB POC's and hand-maintained (researched 29 Sep 2026). It covers:
  * what KPCL is: who owns it, why it exists, its head office, CIN, milestones since 1970 and the laws and regulators it answers to;
  * its subsidiaries, RPCL and KPCGPCL;
  * a profile of each of the 33 stations;
  * the laws, regulators and rules: the Electricity Act 2003, the Karnataka Electricity Reform Act 1999, KERC, CEA, KPTCL, SLDC, the ESCOMs and the emission norms;
  * the Sharavathi pumped storage project;
  * a small glossary.

  Each entry lists its sources. Figures about generation, outages, coal or reservoirs never come from this file.

  `app/knowledge.py` answers from it deterministically, with no model call:
  * `primary()` answers general questions.
  * `fallback()` answers when the model declines or is unreachable.
  * A question that names a period, a measure or a unit, or asks for counts, lists or rankings, always goes to the data.
  * A plain "what is X?" that the file defines wins over the data glossary's nearest term ("what is CEA?" gets the Authority, not "CEA target").

  Knowledge answers read like any other answer. They show no sources, site names, web addresses, "general knowledge" chip or "from the knowledge base" note. The sources stay in the YAML for maintainers only. `guardrails.strip_provenance` enforces this on every knowledge answer, and a test renders every entry to check it. `tests/test_knowledge.py` also checks that every station's capacity and unit count in the file match the warehouse.
* **Period overview** (`reports.period_overview`). A question that names only a month, with nothing else ("What was in March 2024?", "March 2024", "How was March 2024 for RTPS?"), gets that month's overview:
  * generation against the same month a year earlier;
  * generation by source;
  * thermal PLF;
  * the largest generators;
  * the CEA target and how many stations fell short;
  * forced outage hours and trips;
  * KPCL's share of state consumption and the state peak.

  A question that names only an FY gets the FY summary. The screen now treats a month-with-year or a bare FY ("2023-24") as in scope, so these are no longer refused as off-topic.

## Guardrails

The guardrails come from the WB and Cescom POCs plus the problems found by the live stress test. They sit in four layers.

**1. Before any routing (`app/guardrails.py`, fixed wording for every refusal)**
* Empty or over-long questions, pasted SQL or code, and prompt injection are refused.
* Questions about the system itself (database, tables, model, prompt, API keys) are refused.
* Off-topic and general-knowledge questions are refused.
* Requests to change data are refused. The database is read-only anyway; this gives a clear message instead of a figure.
* Anything that could damage plant, endanger people or disrupt the grid is refused (sabotage, disabling interlocks, hacking SCADA).
* Personal data about staff is refused (names, phone numbers, salaries).
* Clock-time questions ("2 pm to 3 pm") get the "hourly data not loaded" note instead of a daily total.
* Unknown stations or units are declined by name ("RTPS has no unit 12: it has 8 units").

**2. On the model's SQL (`app/sql_safety.py`, `app/pipeline.py`)**
* The SQL must be one SELECT over `mart_*` tables only.
* Grain guards apply: no average or sum of ratios or levels, and no mixing of generation grains.
* KPI-named columns must use their registry formula.
* A `company_code` filter the question didn't ask for is rejected. It dropped YTPS (RPCL) and YCCPP (KPCGPCL), giving "99 units" and "7.15 GW" instead of 103 and 9.12 GW.
* One repair round is fed the exact error.
* A false "that period is outside the data" refusal is corrected once and retried.
* A reply cut off at the token limit (the model looping over every column) is retried once with a note and different sampling.

**3. At the database (`app/db.py`)**
* The file is opened read-only (`mode=ro`) with `query_only` set.
* An authorizer allows reads of `mart_*` tables only.
* Every query has a timeout and a row cap.

**4. On results and answers**
* An impossible result is sent back for repair, never shown: PLF, PAF or share above 100% or below 0, or negative generation, stock or hours.
* A subject mismatch is caught ("how many units" answered by SQL that never touches units).
* Internal names are scrubbed from model-written text, and the model's own refusal text is shown only if it is entirely about the data.
* Unit conversions are added deterministically; the original figure always stays.

**5. Second model pass (`app/review.py`, on by default: `KPCL_VERIFY_ANSWERS=true`)**
* **What it checks:** every model-written answer, before it is shown. An independent reviewer call gets the question, the recognised entities, the SQL, the docs of the columns it uses, the result rows and the answer text.
* **What counts as an error:** the reviewer must name one category: wrong measure, scope, period or aggregation, or an unsupported claim. Style, rounding and wording never count.
* **Deterministic veto:** a rejection is overruled when the code can prove it false. That covers:
  * a scope or period the SQL provably filters on;
  * the registry formula used for the measure named;
  * a result shape that matches the question;
  * figures written from the rows by the narrator;
  * any demand for a company filter.
  In live testing the model-as-reviewer raised false objections on 23 of 213 correct answers before this veto existed.
* **After a rejection:** one repair round is fed the reviewer's objection. An answer rejected again is not shown: no figure, table or chart, just a "couldn't confirm" message.
* **If the reviewer is unreachable:** the answer, which already passed every deterministic guard, is shown and the event logged. Metric lookups, verified answers and reports are reviewed code and are not re-checked.
* **Live results:**
  * The stress bank stays at 213/213 with the reviewer on.
  * `python tests/run_review_check.py` catches 6 of 6 deliberately wrong answers (PLF for PAF, wrong station, wrong year, daily rows for a monthly total, an invented claim, MU for %) and accepts 4 of 4 right ones.
  * Added time: median 0.16 s per model-written answer.
* **Where it shows:** in the chat a "Double-checking the answer" step appears while it runs; admins see the verdict in the technical details.

**Server (`app/protection.py`, `app/main.py`)**
* At most 60 questions or dashboard calls per client IP per minute (429 after that).
* At most 8 questions answered at once; the rest wait up to 20 s, then get a polite "busy" message.
* Request bodies are capped at 16 KB (413).
* Security headers go on every response: no framing, no MIME sniffing, no referrer, and a content-security policy that allows only this server, cdnjs and Google Fonts.
* SQL, timings and error details are shown only with `KPCL_SHOW_TECHNICAL_DETAILS=true`.

**Fixes from the 29 Sep 2026 live check** (model live, all 213 stress questions passing before and after):
* **A level asked over a period** means reservoir level, storage or energy potential for a month, an FY or a water year. The answer is now the closing position (the last day with a value in that period), plus the period's low, high and average, answered deterministically by `metric_lookup`. Before, it went to the model, which averaged the daily levels. The reviewer's objection to that was overruled because the result had the right shape.
* **A template that writes one value slot twice** ("… in {storage_pct} was {storage_pct}") has the slot after "in / for / during" replaced by the period named in the question. If the slot is still repeated, the plain figures are shown instead (`answer._repair_repeated_slots`).
* **A shortfall stored as a negative number** (actual minus target) is narrated by its size. "Highest shortfall" now names the biggest miss, not the smallest (`answer._shortfalls_as_positive`).
* **Office holders.** Questions like "Who is the MD of KPCL?" get a note that current office holders are not kept here, with a pointer to KPCL's website. Names are never guessed.

## Live stress test

`python tests/run_stress.py` (server running) asks the 213 questions in `tests/stress_bank.py` on the live server, in the WB POC's two styles.
* **Scope:**
  * Every station, every FY from 2015-16 to 2025-26, and every category (thermal, major and minor hydro, gas, waste-to-energy, solar, wind).
  * Units, days, months, quarters, seasons, calendar years and water years.
  * Coal, reservoirs, grid, outages, BTF, back-down and targets.
  * Unit conversions (GWh, kWh, MWh, crore, lakh and billion units, GW, lakh tonnes, TMC, feet to metres) and name, typo and time edge cases.
  * 16 broad client-style questions.
  * 35 must-refuse and 13 must-allow guardrail cases.
* **Checking:** each expected figure is recomputed from the warehouse by an independent query and must appear in the answer. Generation is recomputed from the unit-daily table, not the station roll-ups the chatbot mostly uses.
* **Output:** results are written to `tests/stress_report.json` and `../../STRESS_TEST_QUESTIONS.md`.
* **Result:** the first run scored 135/182. Every failure was fixed at its cause, and the result is now **213/213** in a clean run.
* **Pacing:** the runner paces itself to stay under the rate limit.
* **Reports:** broad questions are answered by 14 reviewed, parameterised reports in `app/reports.py`, such as the station scorecard, stations needing attention, units at risk, coal situation, reservoir position and FY summary.

## Tests (`kpcl-ai-app/tests`)

`python -m pytest -q` — 327 tests, ~13 s, no gateway needed. `tests/test_stress_fixes.py` covers every fix and guardrail added by the stress test; `tests/test_answer_review.py` covers the second model pass (approve, reject then repair, reject twice, unreachable, veto of false objections). The rest of the suite covers: database guard (writes and non-mart reads refused at the SQLite level), SQL and grain guards, router, **14 golden figures from the K-eRP reports** answered without the LLM (e.g. RTPS FY 2024-25 = 8,310.351 MU, RTPS PLF March 2025 = 64.95 %, RTPS-U2 on 06-Apr-2025 = 4.008 MU), every example and verified answer, guardrails, answer writing, the full pipeline with a mocked LLM (repair round, unsafe SQL, offline mode, every deterministic route), SSE streaming (stages in order, chunks join to the exact answer), the dashboard API (golden figures, totals add up, unknown or hostile FY/station values are 404), and input integrity.

`python tests/run_eval.py` — live evaluation through the real gateway (VPN): 15 questions phrased unlike the verified library, each checked against its reference SQL. Current result **15/15**; LLM answers 0.6–3 s, deterministic answers ~30–50 ms. The first live run scored 10/15; the five failures were fixed at their cause (missing synonyms, coal received vs consumed, aggregate in WHERE, duplicated units) and are now covered by tests.
