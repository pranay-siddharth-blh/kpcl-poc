# KPCL GMS NL2SQL POC — Project Handoff

Written for: a new Claude Code account (or engineer) taking over this project cold. Part A is everything needed to run, test and extend the current app and to work the way the user expects. Part B (sections 1–13 and the appendices) is the original PostgreSQL design reference, unchanged.

Folder: `D:\KPCL POC\KPCL POC\` (note the nested folder name). Last updated **2026-09-30**.

---

# Part A — Current state (start here)

## 0.1 What exists now, in one paragraph

A working **KPCL Generation Management System assistant** for Government of Karnataka officials. It is a FastAPI web app with three pages: a **dashboard** (`/`), an **insights** page (`/insights`) and an **AI-mode chat** (`/chat`). Every figure comes from a read-only SQLite warehouse (`warehouse/kpcl_gms.db`) built from the synthetic 10-year workbook. The dashboard and every chat answer that comes from the data can be **viewed on screen** and **downloaded as an Excel report**. From the dashboard the Excel is a full "MIS report" of the selection; from the chat it is either the MIS report or a report for that answer. General questions about KPCL's organisation, rules, policies, laws and projects are answered from a curated knowledge-base YAML researched from KPCL's official website. All data is proof-of-concept: only the RTPS and SGS figures pinned to K-eRP reports are real (see Part B §8).

## 0.2 Run, test, verify

```powershell
# start (from anywhere; frees port 8000 first, then serves http://127.0.0.1:8000)
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Set-Location "D:\KPCL POC\KPCL POC\webapp\kpcl-ai-app"; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# first time on a new machine only
cd "D:\KPCL POC\KPCL POC\webapp\kpcl-ai-app"; python -m pip install -r requirements.txt

# tests (offline, no gateway needed): 548 pass as of 2026-09-30
cd "D:\KPCL POC\KPCL POC\webapp\kpcl-ai-app"; python -m pytest tests -q -p no:cacheprovider

# live stress test against the RUNNING server (213 questions, checks figures against the warehouse): 213/213
python tests\run_stress.py --workers 4        # rewrites tests/stress_report.json and ../../STRESS_TEST_QUESTIONS.md

# live evaluation through the LLM gateway (needs the VPN): 14/15, see 0.9 for the one miss
python tests\run_eval.py
```

- Pages: `/` dashboard · `/insights` · `/chat` (AI mode) · `/health`.
- **The server does not auto-reload.** Restart after any code change, and after any change to `knowledge_base.yaml` or the semantic layer (both are cached in memory with `lru_cache`).
- The error `ModuleNotFoundError: No module named 'app'` means uvicorn was started from the wrong folder. Always run it from `webapp\kpcl-ai-app`.
- `webapp/.env` holds the internal vLLM gateway key (copied from the WB POC). **It is a secret: never print it, commit it or send it anywhere.** `webapp/.env.example` lists the settings (`KPCL_*` in `app/config.py`: timeouts, row cap, rate limit 60/min per IP, concurrency 8, `KPCL_SHOW_TECHNICAL_DETAILS` for SQL/route/timings in the UI).

## 0.3 Folder map

| Path | What |
|---|---|
| `warehouse/` | SQLite `kpcl_gms.db` (layers `src_ ref_ raw_ stg_ core_ mart_ semantic_ ledger_`), build scripts, 71/71 anchor reconciliation, `metadata/` (metric registry, capability manifest, known issues KI-01…KI-11). See `warehouse/README.md`. |
| `webapp/kpcl_semantic_layer/` | `semantic_layer.json` (generated), `example_queries.sql` (36 reviewed examples), **`knowledge_base.yaml`** (hand-curated general knowledge, see 0.5 g). |
| `webapp/integrity_manifest.json` | SHA-256 locks on `warehouse/kpcl_gms.db`, `semantic_layer.json`, `example_queries.sql`. `tests/test_integrity.py` fails if they change. Rebuild with the build script if you change them on purpose. `knowledge_base.yaml` is **not** locked. |
| `webapp/kpcl-ai-app/app/` | the app (files below) |
| `webapp/kpcl-ai-app/app/static/` | `dashboard.html/.css/.js`, `insights.html/.js`, `chat.html/.js`, `report-viewer.js/.css`, logos. **All page scripts are separate `.js` files**: the security policy forbids inline scripts (see 0.5 f). |
| `webapp/kpcl-ai-app/tests/` | pytest suites plus the live `run_stress.py` / `run_eval.py` |
| `webapp/README.md` | request flow, frontend, dashboard, semantic layer, guardrails, stress test. **It predates the report features below; this handoff is the up-to-date reference for them.** |

App modules (`webapp/kpcl-ai-app/app/`):

| File | Role |
|---|---|
| `main.py` | FastAPI routes, SSE streaming of answers, keeps report answers (`report_id`), 409 handler for withheld reports |
| `pipeline.py` | the question pipeline (order in 0.4) |
| `guardrails.py` / `protection.py` | question screening, refusals, provenance stripping / security headers, CSP, rate limit, body cap |
| `semantic_layer.py`, `prompts.py`, `llm.py`, `sql_safety.py`, `db.py`, `review.py` | entity extraction, prompt, the one LLM call, SQL guard, read-only DB with authorizer, answer review |
| `verified_queries.py`, `metric_lookup.py`, `reports.py`, **`breakdown.py`** | deterministic answer routes (no LLM) |
| `answer.py` | deterministic answer writer, number formats, unit conversion, charts, tables |
| `knowledge.py`, `capability.py` | knowledge-base answers / "not in this data" notes |
| `dashboard_data.py` | fixed reviewed queries behind the dashboard and insights, the single source for every dashboard figure |
| **`mis_report.py`** | dashboard selection → Excel MIS workbook; `finalize()` safety pass; `data_version()` |
| **`answer_report.py`** | keeps chat answers, builds the answer workbook (or the MIS workbook for summaries), checks |
| **`xlsx_view.py`** | renders a workbook (the same object as the download) to HTML + chart data for the on-screen viewer |

## 0.4 How a chat question is answered (`app/pipeline.py`, in order)

1. `guardrails.screen_question`: hard refusals for empty, too long, code, injection, harmful, write requests and personal data (e.g. names or salaries of people).
2. Identity question, then **verified queries** (hand-checked SQL).
3. Knowledge-base definitions and the data glossary.
4. `knowledge.primary` (general questions) vs `capability.check` (data this system lacks: billing, JMR, HR records, hourly…). A question about KPCL's own organisation, policies or publications keeps the knowledge answer over a capability note, unless it asks for money or records (`_MONEY_OR_RECORDS`). A request for figures ("KPCL summary", "RTPS overview") skips the company profile (`reports.FIGURES_ASK`) unless it is about the organisation (`reports.PROFILE_ASK`, `knowledge.is_about_organisation`).
5. Off-topic refusal, entity extraction, unknown station/unit names, out-of-range periods.
6. **Reports** (`reports.match_and_run`): `period_overview`, `unit_status`, `entity_list`, `units_at_risk`, `attention`, `coal_situation`, `reservoir_position`, `hydro_wy_compare`, `thermal_vs_hydro`, `share_trend`, `outage_trend`, `month_extreme`, `thermal_efficiency`, `station_scorecard`, **`generation_breakdown`**, `fy_summary`. A report is skipped if it does not cover a measure the question names (`covers_asked_measures`). If no period was named, the answer says FY 2024-25 was used (`DEFAULT_PERIOD_NOTE`).
7. `metric_lookup` (single figures), then the **LLM path**: pruned semantic layer → one structured call → SQL safety (mart_* reads only) → read-only DB → deterministic writer → answer review.
8. Unit conversion (GWh, TWh, lakh tonnes…) is appended to figures when asked. Every answer computed from the data then gets a `report_id` (see 0.5 c).

## 0.5 Features added (2026-09-29 → 2026-09-30)

**a. Dashboard and Insights** (`dashboard_data.py`, `dashboard.*`, `insights.*`): KPIs, monthly/weekly/daily generation toggle, station league, units out, coal, reservoirs, losses, ten-year trend, station drill-down, Kannada/English, AI-mode transition. The weekly/daily conventions are the source of truth: weeks run Monday–Sunday, a week belongs to the month of its middle day, and it is compared with the week 364 days earlier.

**b. Excel MIS report from the dashboard** (`mis_report.py`, `GET /api/mis-report?fy=&station=&grain=monthly|weekly|daily|all`). A gold button group in the dashboard header: **View**, **MIS report XLSX** (downloads what is on screen: FY, station and period toggle) and a menu (*As on screen* / *Complete report*, each with its own View). Sheets:
- Summary (letterhead, KPIs vs the previous FY, position as on the last date, observations, integrity checks)
- Generation – Monthly / Weekly / Daily
- Stations with category subtotals (all-KPCL), or Units (one station)
- Station × Month, Ten-year trend, Generation loss, Units out of service, Coal stock, Reservoirs, Notes

Every figure comes from `dashboard_data.build_dashboard`, so the report cannot disagree with the dashboard. Integrity checks (months, weeks, days, stations and units add up to the year) are printed on the Summary sheet.

**c. AI mode "Download report"** (`answer_report.py`). **Every answer computed from the data** gets **View report** and **Download report XLSX** buttons. The user rejected a word-list trigger after "Monthly KPCL generation trend" got none. The server keeps the answer in memory under a random `report_id` (3 hours, 500 max) and builds the file from that, never from anything the page sends.
- **Summary answers** (`report:fy_summary`): the full MIS workbook for that FY and station. "weekly", "daily" or "complete" in the question picks the sheets.
- **Everything else**: an answer workbook with a Report sheet (question, findings as answered, key figures, dashboard KPIs as context when one FY and at most one station are named, checks), a Data sheet (every row, with a total row for time series of summable measures), a Chart sheet (a native Excel chart) and a Notes sheet.
- Endpoints: `GET /api/answer-report/{id}` and `.../{id}/view`.

**d. Generation breakdowns** (`breakdown.py`, route `report:generation_breakdown:<grain>`). Daily, weekly, monthly, quarterly, yearly, station-wise and unit-wise generation or PLF for KPCL or one station, for an FY (or one month for daily/weekly), all from `build_dashboard`, so chat answers match the dashboard. It returns a total row from the year's own figures, which the Excel checks against. It never fires for other measures (`_OTHER_MEASURE`), rankings or comparisons (`_NOT_A_BREAKDOWN`), or questions without generation/PLF wording. `answer.summable_over_time` stops stock, level and storage figures being "totalled" over time.

**e. View report** (`xlsx_view.py`, `static/report-viewer.js/.css`). A full-screen Excel-style preview rendered from the very workbook object the download writes:
- sheet tabs, column letters and row numbers, frozen header row
- the file's own number formats (Excel rounding), fills, merged cells and conditional colours
- charts via Chart.js at their anchor cells
- zoom, the printed draft footer, and its own Download button

Viewer rules learned the hard way: a table needs an explicit pixel width for its column widths to hold; charts can be anchored past the last used column; unwrapped text spills into empty cells as in Excel.

**f. Report guardrails** (`tests/test_report_guards.py`):
- `mis_report.finalize()` runs on every workbook, downloaded or previewed:
  - **no formulas**: text starting `= + - @` is stored as text with Excel's quote prefix, because openpyxl otherwise writes `=...` as a live formula;
  - a **"Proof of concept – not for official use"** footer on every printed sheet, plus DRAFT file properties;
  - the checks travel with the workbook.
- **A workbook that fails any check is withheld** (`ReportUnderReview` → HTTP 409 `under_review`), with an "under review" notice on the dashboard, in chat and in the viewer. The checks include the add-up checks and "the figure stated in the answer equals the Data total / MIS figure".
- Cut-short results get a red **INCOMPLETE RESULT** banner on the Report sheet.
- Reports carry a **data version** (a hash of the warehouse file). Kept answers expire when the warehouse changes.
- A "Period used" note appears when FY 2024-25 was a default. A "Units" note appears when the answer converted units (the Excel stays in MU etc.).
- The chat CSV download escapes formula-like text.
- Page scripts were moved out of the HTML, and the CSP no longer allows `'unsafe-inline'` scripts. Inline styles are still allowed.

**g. Knowledge base expansion** (`webapp/kpcl_semantic_layer/knowledge_base.yaml`, `app/knowledge.py`). New groups:
- `organisation`: hierarchy/organogram, Board, board committees, MD and full-time directors, management levels and cadres, decision-making, offices and hours, vigilance, Energy Department.
- `policies`: service rules and HRD Manual, CSR policy, whistle-blower policy, recruitment, RTI, procurement.
- `public_information`: what KPCL publishes, awards, consultancy.

It also has a new company (KECML / Baranj mines), new regulation entries (Companies Act 2013, CAG audit, fly-ash rules, Coal Mines (Special Provisions) Act 2015), new projects (proposed projects, Godhna, Varahi pumped storage, Shivanasamudra seasonal) and glossary terms. Most of this was researched on 2026-09-30 from **kpcl.karnataka.gov.in**: the organogram updated 25-May-2026, the RTI 4(1)(b) handbook of Sept 2024, the PIO list of 28-Jul-2026, the CSR policy of 08-Apr-2022 and the Whistle Blower policy of 07-Jan-2020, plus the About, Mission, Awards, Projects, Joint Ventures, Careers, Consultancy and Environment pages. A few facts came from other sources, all listed under each entry's `sources`: the Baranj mine history (Global Energy Monitor and court records), the Mandakini-A block (CARE Ratings) and the Civil Designs post title. The official PDFs are mostly scans; the web-fetch summariser **hallucinated** on them, so text was extracted with PyMuPDF and images were read directly. Matching now picks the **longest matching alias** across groups (`_best_entry`). KPCL has a separate `mission` facet. Organisation questions phrased "how many / list…" are not treated as data questions.

## 0.6 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/`, `/dashboard`, `/insights`, `/chat`, `/health` | pages, liveness |
| GET | `/api/dashboard?fy=&station=` / `/api/insights?fy=` | fixed reviewed queries; unknown FY or station is a 404 |
| POST | `/api/ask` (SSE: status → answer_chunk → chart → table → done{report_id}) / `/api/ask_once` (JSON) | the chat |
| GET | `/api/mis-report?fy=&station=&grain=` · `/api/mis-report/view?...` | Excel MIS / its preview JSON; 409 `under_review` if a check fails |
| GET | `/api/answer-report/{id}` · `/api/answer-report/{id}/view` | Excel for a kept chat answer / its preview; 404 when expired or the data changed; 409 when withheld |

All `/api/ask*`, `/api/dashboard`, `/api/insights`, `/api/mis-report*` and `/api/answer-report*` calls share the per-IP rate limit.

## 0.7 Verification status (2026-09-30)

- **pytest: 548 passed.** Report-related suites: `test_mis_report.py`, `test_answer_report.py`, `test_report_view.py`, `test_report_guards.py`, and `test_knowledge.py` (it covers the new organisation answers). `tests/conftest.py` lifts the rate limit for the suite (`KPCL_RATE_LIMIT_PER_MINUTE`); the limit itself has its own test.
- **Stress (live): 213/213. Eval (live, VPN): 14/15.**
- The golden figures still reproduce K-eRP: RTPS FY 2024-25 8,310.351 MU, PLF 55.16%; RTPS Mar-2025 831.212 MU, PLF 64.95%; KPCL FY 2024-25 37,602.179 MU.
- Checked in a real browser (headless Edge driven over the Chrome DevTools Protocol with small Python scripts, which were not kept): dashboard View/Download/menu, chat View/Download, phone width, no CSP violations or page errors, the Insights page. Downloaded files were opened in **real Excel** via COM and exported to PDF to check layout and that nothing was flagged as corrupt.

## 0.8 How the user wants you to work (must follow)

- **`D:\WB POC` and `D:\Cescom POC (2)` are strictly read-only reference projects.** Read them only; never write, build, install or run anything that writes there.
- **The audience is government officials.** The UI must stay formal and restrained: KPCL navy, a muted gold accent, serif headings. There are no text-size or high-contrast controls, and the model status stays a small dot in the footer.
- **Answers never show where they came from**: no "Sources:", URLs, site names, "general knowledge" chips or "from the knowledge base" notes (`guardrails.strip_provenance` enforces this; the sources stay in the YAML for maintainers).
- **Never name KPCL's current office holders** (they change); answers point to KPCL's official website. Tests enforce this.
- **Figures must be exactly right and match the dashboard and the raw data.** Build new report paths from `dashboard_data` or reviewed SQL, add integrity checks, and never let the LLM type numbers into reports.
- Give the full PowerShell terminal command when asked how to run something (the user runs it themselves).
- The user often says "brainstorm / only list, do not implement". In that case give ideas in chat and write no code. They test by hand and report back; keep a short list of test questions ready.
- Confirm before anything outward-facing. Work fast ("asap") but verify: tests, the live stress run, and a browser check for UI changes.

## 0.9 Gotchas for the next agent

- **Windows.** Git Bash heredocs that contain apostrophes (for example "Karnataka's") fail with "unexpected EOF". Write Python edit scripts to files in the scratchpad and run them, as was done throughout. PowerShell here is 5.1: no `&&`; use `;`.
- **Tool outages.** The auto-mode safety classifier sometimes returns "no verdict" for Bash/PowerShell; after 10 in a row the turn stops. Do read-only work (Read/Grep) meanwhile, then retry.
- **Known eval miss (#11).** "How many days in FY 2023-24 was Linganamakki below 30% storage?" gets the closing storage from `metric_lookup`'s level-over-period path instead of a count of days. This was confirmed to predate these changes. Not fixed yet.
- **KPCL's own published generation records conflict with the POC data** (e.g. "highest 29,784 MU in 2014-15"). They are deliberately kept out of the YAML, and a test enforces it.
- Organogram abbreviations that could not be confirmed (RO(C), RO(P&C), RO(EM), GM (A), AGM (CC), AGM (HRD) CHT, Resident Engineer (ND)) are described without expansion. Confirmed: TD/ED/CD = thermal/electrical/civil designs, S&K = Systems & KERP, CA&CS = Corporate Affairs & Company Secretary, IA = internal audit, LS = legal services, MCB = Mandakini coal block, BOCM = Baranj open cast mines.
- `run_stress.py` overwrites `tests/stress_report.json` and `STRESS_TEST_QUESTIONS.md`. `webapp/server*.log` files are leftovers from earlier runs.
- The user's persistent notes (memory) live in `C:\Users\hp\.claude\projects\d--KPCL-POC\memory\`, which is tied to that account. A new account will not see them; section 0.8 restates what matters.

## 0.10 Open items and suggested next steps

1. Fix eval #11 (count of days a level is above or below a threshold).
2. Remaining report guardrails that were proposed but not built:
   - limit concurrent report builds and cache workbooks per (FY, station, grain, data version);
   - cap the memory for kept answers by size;
   - put the letterhead only on reviewed (not unreviewed LLM) answers;
   - bind `report_id` to a session;
   - an audit trail and a reference number on each report;
   - a same-site check on the report endpoints;
   - page the preview for very large sheets;
   - say that reports are English-only, or add Kannada;
   - authentication and roles before deployment beyond one machine.
3. Update `webapp/README.md` with sections 0.5 b–g (it predates them).
4. AI-mode MIS ideas the user was given (not built): ask for an MIS report in chat by name; a one-page "brief" in plain language; an exception report ("what needs attention"); period comparison with causes; follow-up questions on a report; saved report packs and schedules; Kannada and print-ready PDF; a reference number and checks on every report.
5. Still open from Part B: the hydro major/minor split (9.1, on hold at the user's request), partial reconciliation (9.2), the 06-Apr coal inconsistency (9.3), and the second workbook not loaded (9.6).

## 0.11 Part B issues and how they stand

- **Hydro split (9.1):** on hold at the user's request. Affected answers carry a caveat.
- **Partial reconciliation (9.2):** unchanged. Answers say when a figure is pinned to the report.
- **Coal inconsistency on 06-Apr (9.3):** unchanged, with a caveat.
- **Empty facts (9.4):** no longer exposed. The chatbot says the data isn't loaded instead of returning empty results.
- **Ratio outliers (9.8):** flagged and guarded against averaging.
- **RTPS cumulative anchors:** look shifted by one unit (KI-08).
- **Second workbook (9.6):** still not loaded.

---

# Part B — Original design reference (written 2026-09-29, before the chatbot existed)

Sections 1–13 below describe the original PostgreSQL design, which Part A's SQLite warehouse and app were built from. Where they say no chatbot exists, Part A supersedes them.

> **How this was verified (original).** Every script, SQL file and JSON was read in full. Both Excel workbooks were read with openpyxl/pandas (all 21 + 6 sheets, all 365,132 daily rows profiled). Numbers quoted below were recomputed from the workbook unless marked "as recorded". **PostgreSQL is not installed on this machine, so no database step was re-run**; database-side results (71 PASS in `reconciliation_report.json`, row counts in view comments) are as recorded by the original author. The Word document is 86 screenshots; its text was read and the HO Daily Generation Report, RTPS Daily Generation Report and GMS dashboard images were viewed, not all 86.

## 1. What this project is, in one paragraph

Karnataka Power Corporation Ltd (KPCL) runs an ERP called **K-eRP** with modules HRMS, **GMS** (Generation Management System), DMS (defects), IIMS (inventory), WIMS, FMS, F&A. This POC builds a **natural-language-to-SQL (NL2SQL) chatbot foundation for GMS**: a PostgreSQL schema modelled on the K-eRP GMS screens/reports, a **10-year synthetic dataset** that is pinned to real figures read off the K-eRP document screenshots, a **star-schema view layer (`gms_mart`)** that is easy for an LLM to query, and a **generated semantic layer JSON** (table/column comments, metric formulas, synonyms, few-shot question→SQL pairs) meant to be loaded into the chatbot prompt or retrieval index. (When this was written no chatbot existed; one now does. See Part A.)

**All data is synthetic.** Both workbooks say so in their README. Only the "anchor" values in `document_anchors.json` are real (transcribed from screenshots), and the database is forced to match them for a limited set of periods (section 8).

## 2. Data flow

```
erp_at_kpcl_hrms.docx (screenshots of real K-eRP screens)
      │  (manual transcription)
      ▼
document_anchors.json ───────────────────────────────┐
                                                     │
KPCL_GMS_10yr.xlsx (synthetic, 21 sheets)            │
      │  load_gms.py  (12 sheets → 23 tables)        │
      ▼                                              ▼
PostgreSQL db "gms", schema gms  ◄── gms_schema.sql   reconcile_to_document.py  (overwrites/rescales daily rows)
      │                                              │
      │  gms_mart.sql (30 views)                     ▼
      ▼                                        verify_reconciliation.py → reconciliation_report.json (71 PASS)
schema gms_mart  ──►  build_semantic_layer.py + nl2sql_question_bank.sql ──► gms_semantic_layer.json  (chatbot input)

KPCL_33_Projects_10Y_Joint_Meter_and_MPR.xlsx  → NOT connected to anything (see 5.2)
```

## 3. File inventory

| File | Size | Role |
|---|---|---|
| `erp_at_kpcl_hrms.docx` | 10.3 MB | Source document: 425 paragraphs, 1 table, 86 embedded images. Intro text about KPCL (inception 1970, 8,738.305 MW: 5,020 MW thermal, 3,679.75 MW hydro, 4.555 MW wind, 34 MW solar, 370 MW combined cycle "on the verge of commissioning"). Sections: HRMS, **GMS**, DMS, IIMS, F&A. HRMS covers 3,692 employees, 7,342 pensioners, 24 projects, 363 divisions. The GMS screenshots (images ~30–44) are where every anchor value comes from. |
| `document_anchors.json` | 31 KB | Hand-transcribed numbers from the screenshots. Ground truth for reconciliation. See 8.1. |
| `KPCL_GMS_10yr.xlsx` | 41.7 MB | Main synthetic dataset, 21 sheets, 01-Apr-2015 to 30-Apr-2025. See 5.1 and Appendix A. |
| `KPCL_33_Projects_10Y_Joint_Meter_and_MPR.xlsx` | 1.3 MB | Second, independent synthetic workbook (monthly JMR + MPR, Apr-2016 to Mar-2026). Unused by any script. See 5.2 and Appendix B. |
| `gms_schema.sql` | 51 KB | Core normalised schema `gms`: 47 tables + 10 reporting views + comments. |
| `gms_mart.sql` | 25 KB | Star-schema view layer `gms_mart`: 30 views (dims + facts), all comments. |
| `gms_schema_erd.png` / `gms_mart_star_erd.png` | 2.2 MB / 0.7 MB | ERD pictures of the two schemas. The mart ERD shows dimensions (blue), core facts (dark green), derived roll-up facts (grey). |
| `load_gms.py` | 12 KB | Loads `KPCL_GMS_10yr.xlsx` into `gms`. |
| `reconcile_to_document.py` | 18 KB | Adjusts loaded rows to match the anchors. |
| `verify_reconciliation.py` | 8 KB | Re-derives anchors from the DB and writes `reconciliation_report.json`. |
| `reconcile_log.txt`, `reconciliation_report.json` | 3 KB / 11 KB | Output of the last run: 71 checks, 71 PASS, 0 FAIL, 0 SKIP. |
| `nl2sql_question_bank.sql` | 11 KB | 24 natural-language questions with reference SQL against `gms_mart`. |
| `build_semantic_layer.py` | 10 KB | Builds `gms_semantic_layer.json` from live DB comments + hardcoded metrics/synonyms + the question bank. |
| `gms_semantic_layer.json` | 65 KB | Output: 30 tables/views with column comments, 14 metrics, 19 synonyms, 4 join rules, 24 examples. |

## 4. How to run it end to end

Prerequisites: PostgreSQL 15+ (schema uses `UNIQUE NULLS NOT DISTINCT`, which needs 15), Python with `pandas`, `psycopg2`, `openpyxl`. All scripts default to DSN `postgresql://postgres@localhost:5432/gms` (no password) and read files **relative to the current directory**, so `cd` into the folder first.

```
createdb gms
psql gms -f gms_schema.sql
python load_gms.py "postgresql://postgres@localhost:5432/gms" KPCL_GMS_10yr.xlsx     # reads 12 sheets; minutes, 41 MB file
psql gms -f gms_mart.sql
python reconcile_to_document.py                     # writes reconcile_log.txt
python verify_reconciliation.py                     # exits 1 if any check FAILs
python build_semantic_layer.py                      # writes gms_semantic_layer.json
psql gms -f nl2sql_question_bank.sql                # optional: smoke-test the 24 reference queries
```

Order matters. `reconcile_to_document.py` rescales relative to current values, so re-running it after a successful run is harmless (factors become ×1.0000 and Step 1 rewrites the same values). `load_gms.py` is **not** re-runnable on a loaded database (COPY hits primary-key violations); reload into a fresh database.

## 5. The two workbooks

### 5.1 `KPCL_GMS_10yr.xlsx` (the one that matters)

Sheet names equal table names in `gms_schema.sql`. Scope: 33 stations, 103 units, 3,683 calendar days (01-Apr-2015 … 30-Apr-2025), **365,132 rows** in `daily_unit_generation` (one per unit per day; units commissioned later have fewer rows, e.g. YCCPP-U1 starts 01-Jul-2024 = 304 rows). Every row's `data_source` is `SYNTHETIC`.

How the values were built (from its own README, sheet `README` A8:A16):
1. **FY 2024-25 is the anchor year**: RTPS and SGS use real unit-level FY and March-2025 figures; other stations use FY estimates derived from the HO report (`WY − FYtd + LastFYtd × 61/6`, see sheet `anchors` column D).
2. Every earlier year is 1–5 % different from the following year (drawn 1.2–4.8 %), all units of a station move together, same-month values follow the same rule. This is why totals look smooth: KPCL gross by FY (recomputed): 2015-16 30,148 → 2016-17 33,078 → 35,709 → 38,488 → 37,565 → 37,540 → 37,357 → 36,589 → 36,482 → **2024-25 37,602 MU**.
3. Auxiliary %, specific coal, GCV, specific oil, CEA targets, dam storage and state demand follow the same rule.
4. Daily shape comes from season, weekday and outages; planned maintenance is in the same month each year.
5. Exempt events are listed in sheet `exceptions` (RTPS-U1 tripped since 10-Aug-2022 09:00, RTPS-U4 since 09-Nov-2024 15:58, SIRWAR-U1 since 01-Jun-2023 [assumed], BTPS-U3 boiler tube failures Jan-2024, April-2025 outages of YTPS-U2 and BTPS-U3, approximate commissioning dates for late units, Kalmala/Ganekal below reporting resolution).

Sheet groups:
- **Provenance/QA** (not loaded): `README`, `YoY_check` (SUMIFS formulas + colour rules, 33 stations × 10 FYs), `validation` (20 PASS checks), `anchors` (51 rows: station, what was anchored, value, document source), `exceptions` (18 rows).
- **Roll-ups** (not loaded, derived from daily): `annual_station_summary` (308 rows), `annual_unit_summary` (997), `monthly_station_summary` (3,684), `monthly_unit_summary` (11,997).
- **Masters** (loaded): `projects` (33), `units` (103), `calendar` (3,683), `reservoirs` (6).
- **Facts** (loaded): `daily_unit_generation` (365,132), `unit_outages` (5,877: 4,623 FORCED, 975 PLANNED, 279 RSD; 347 boiler tube failures; 7 structural; 3 still open), `generation_loss` (185,988; RTPS 109,379, BTPS 49,753, YTPS 25,624, YCCPP 1,232), `generation_targets` (11,997 = 103 units × 117 months; `kerc_approved_mu` is null in every row), `coal_stock` (10,321 rows for RTPS, BTPS, YTPS), `coal_receipts` (22,300), `reservoir_levels` (22,098 = 6 × 3,683), `daily_grid_summary` (3,683).

The complete column-by-column map with cell addresses, types, null counts and ranges for every sheet is in **Appendix A**. Full cell dumps of the small sheets (`README`, `validation`, `anchors`, `exceptions`, `projects`, `units`, `reservoirs`) are also there.

Things worth knowing about the data:
- `projects!E` (plant_category) puts **14 hydro stations in MAJOR HYDRO** and only 7 in MINOR HYDRO. The real HO dashboard counts only SGS+NPH+VUGPH as MAJOR (see 9.1).
- `units` has cumulative opening balances (`opening_cum_gen_mu` etc., as on 31-Mar-2015) **only for RTPS-U1…U8**; all other units are blank, so `v_unit_cumulative` returns NULL for them.
- `calendar!is_holiday` is False for all 3,683 rows; `holiday_name` is empty. Seasons: SUMMER Mar–May, MONSOON Jun–Sep, POST_MONSOON Oct–Nov, WINTER Dec–Feb.
- `reservoir_levels!energy_potential_mu` is null for reservoirs 2, 5, 6 (TALAKALALE, GERUSOPPA, ALAMATTI have no `energy_at_frl_mu`), 11,049 nulls.
- `daily_unit_generation` fuel columns are null for non-applicable plant types (coal/HFO/LDO null on 319,381 rows; gas 364,524; MSW 364,798).
- Extreme ratio outliers exist on near-zero-generation days: e.g. RTPS-U3 on 2016-08-07 has gross 0.006 MU and HFO 151.79 KL, so `sp_oil_ml_kwh` = 25,298 and `gross_heat_rate_kcal_kwh` = 263,995. Never `AVG()` these columns (the mart documents SUM/SUM for this reason).
- 43 rows have `plf_pct` above `paf_pct` by ≤ 0.02 percentage points (rounding); harmless.
- Units are as in the workbook README: MU = million kWh, MT = tonnes, KL = kilolitres, hours decimal, percent columns 0–100.

### 5.2 `KPCL_33_Projects_10Y_Joint_Meter_and_MPR.xlsx` (orphan)

A separate synthetic workbook: **3,960 rows = 33 projects × 120 months (2016-04 … 2026-03)** in two sheets, `Joint_Meter_Reading` (13 columns, one meter per project, meter no. always 1, cumulative initial→final kWh readings) and `Monthly_Performance_Report` (22 columns). Plus `Project_Master`, `10Y_MPR_Summary`, `Validation_Checks`, `README`. No formulas anywhere (all values). Its README states the reference is the RTPS Joint Energy Meter Report / MPR structure.

I verified it is **internally consistent**: Difference = Final − Initial; each month's Initial equals the previous month's Final (chain error 0.0); Gross = Difference × Constant / 10⁶; Net = Gross − Aux; PLF = Gross / (MW × hours) exactly; JMR gross equals MPR gross on every row; running + planned + forced hours = hours in month.

But it is **not consistent with the main workbook or with reality**, and no script loads it:
- Different generation levels: e.g. RTPS 10,652 MU in 2024 here vs the 8,310.351 MU real FY 2024-25 anchor used everywhere else.
- Different station names and capacities: `KODSALLI`, `GHATAPRABHA` (32.7 MW), `MALPRABHA`, `KG-WER`, `TNL-SOLAR-3`, `BWtEP`; KALMALA 4 MW and GANEKAL 35 MW (main workbook: 0.4 and 0.35 MW; the document HO report shows 0.4/0.35).
- `Heat Rate (kcal/kWh)` (col Q) is exactly 1000 × `Specific Coal Consumption (kg/kWh)` (col T), giving nonsensical specific coal of ~2.5 kg/kWh (real: ~0.8).
- Coal, LDO only for thermal; water only for hydro; heat rate only for thermal and combined cycle.
- Its data would be the natural source for `jmr_statement`/`jmr_meter_reading`, but the schema needs per-meter roles (UNIT_GEN/UNIT_AUX/COMMON_AUX/STATION_EXPORT) and `load_gms.py` explicitly skips JMR.

Treat it as sample material for the JMR/MPR report *structure*, not as data.

## 6. Core schema `gms` (`gms_schema.sql`)

Conventions (header comment): snake_case singular names; `*_code` business key, `*_id` surrogate; unit of measure is the column suffix (`_mu` million kWh, `_kwh`, `_mw`, `_hrs`, `_mt`, `_kl`, `_rs`, `_pct` 0–100, `_mcft` million cu ft, `_cum` cubic metre, `_kcal_kg`/`_kcal_l`/`_kcal_kwh`); base tables hold measured facts only; **ratios live in views as SUM/SUM**; every fact joins `dim_date`.

47 tables in 7 sections. **"Populated"** means `load_gms.py` fills it.

| Section | Tables | Populated? |
|---|---|---|
| 1 Reference & masters | `company`, `plant_type`, `plant_category`, `station`, `fuel`, `unit`, `dim_date`, `reservoir`, `reservoir_station` | Yes |
| 1 (org) | `division`, `employee` | **Empty** |
| 2 Generation ops | `unit_daily_generation` (PK unit_id+gen_date; `net_export_mu` generated = gross − aux; CHECKs: aux ≤ gross, hours sum to 24 ±0.05), `unit_generation_loss`, `backdown_reason`, `outage_reason`, `unit_outage`, `btf_failure_type`, `btf_location`, `boiler_tube_failure`, `generation_target` | Yes |
| 2 (empty) | `unit_hourly_reading`, `station_daily_consumption` (common aux, colony, local feeder) | **Empty** (see 9.2) |
| 3 Fuel | `coal_supplier`, `coal_receipt`, `station_coal_stock` (generated `total_consumed_mt`, `closing_stock_mt`) | Yes |
| 4 Hydrology/grid | `reservoir_daily_level`, `state_grid_daily` | Yes |
| 5 JMR | `energy_meter`, `jmr_statement`, `jmr_meter_reading` (generated `energy_mu`) | **Empty** |
| 6 Commercial | `counterparty`, `allocation_pool`, `escom_share`, `billing_group`, `billing_group_member`, `tariff_rate`, `energy_bill`, `energy_bill_line`, `energy_bill_escom_split`, `commercial_txn_type`, `commercial_transaction`, `tripartite_adjustment`, `seci_calibration_charge` | **Empty** |
| 7 DMS | `asset`, `defect`, `permit_to_work`, `line_clearance` | **Empty** |

So 23 of 47 tables carry data. Ten reporting views (section 8 of the SQL): `v_unit_daily`, `v_station_daily`, `v_unit_monthly` (the MPR unit columns), `v_station_monthly` (with CEA/KERC/internal targets), `v_station_fy`, `v_unit_outage`, `v_unit_current_status`, `v_unit_cumulative`, `v_jmr_reconciliation`, `v_reservoir_daily`. `COMMENT ON` statements at the end are what the semantic-layer builder reads (for `gms_mart`, not `gms`).

Key modelling decisions to preserve:
- **PLF** = Σ gross_gen_mu / Σ capacity_mu × 100, where capacity_mu = capacity_mw × 24 / 1000 per unit-day. **PAF** = Σ declared_capacity_mu / Σ capacity_mu × 100. **Aux %** = Σ aux / Σ gross. **Specific coal** = Σ coal_mt / 1000 / Σ gross. Weighted GCV and heat rate are weighted by coal and by gross respectively.
- Station net export = Σ unit (gross − aux) − `common_aux_mu` from `station_daily_consumption`.
- `deemed_gen_mu` (NLD, no-load-demand) is *derived at load time* from the NO_LOAD_DEMAND rows of `generation_loss`.
- `declared_capacity_mu` is derived at load: `paf_pct / 100 × capacity_mw × 24 / 1000`.
- FY is Apr–Mar (`'2024-25'`); water year is Jun–May (`dim_date.water_year`); month labels are K-eRP style `'MAR-2025'`.
- Open outage: `sync_at IS NULL` ⇔ `outage_hrs IS NULL` (CHECK); views compute live duration to now.

## 7. Semantic mart `gms_mart` (`gms_mart.sql`)

30 **views only** (no copied data), built on `gms` so it can never drift. Design idea: an LLM does better on a few wide tables than a 47-table web, so each fact carries the attributes normally reached via joins, and every table/column has a `COMMENT` that states grain and pitfalls. Every fact has `date_key` (join `dim_date`) and `unit_key` and/or `station_key`.

Dimensions (14): `dim_date`, `dim_station` (33; adds `plant_category_label`, `unit_count`, `fed_by_reservoirs`), `dim_unit` (103), `dim_reservoir` (6), `dim_outage_reason` (UNION of outage reasons with `reason_class='OUTAGE'` and back-down reasons with `'BACKDOWN'`, so you must filter by class), `dim_btf_type`, `dim_btf_location`, `dim_coal_supplier`, `dim_counterparty`, `dim_billing_group`, `dim_division`, `dim_employee`.

Facts (16 + 4 roll-ups + 2 point-in-time), with grain:
- `fact_unit_generation_daily` unit×day (base fact, 30 columns; **ratios not stored**)
- `fact_station_common_consumption_daily` station×day
- `fact_unit_outage` per trip event (BTF columns denormalised; `is_open`)
- `fact_generation_loss_daily` unit×day×back-down reason
- `fact_generation_target_monthly` unit×month
- `fact_coal_stock_daily` station×day (coal stations); `fact_coal_receipt` per rake delivery
- `fact_reservoir_daily`, `fact_grid_daily`
- `fact_jmr_meter_monthly`, `fact_energy_bill_escom`, `fact_commercial_transaction`, `fact_defect` (**all backed by empty tables**)
- Roll-ups: `fact_station_generation_daily / _monthly / _fy`
- Point-in-time: `fact_unit_current_status` ("right now", no date), `fact_unit_cumulative`

## 8. Anchors and reconciliation

### 8.1 `document_anchors.json`

Top-level keys: `_meta`, `ho_daily_generation_report`, `rtps_daily_report_2025_04_06`, `sgs_daily_report_2025_04_06`, `rtps_mpr_march_2025`, `rtps_mpr_fy_2024_25`, `sgs_mpr_march_2025`, `alamatti_jmr_march_2025`, `rtps_jmr_april_2025`, `sale_of_energy_bill_march_2025`, `conflicts_resolved_by_headline_total`.

- `ho_daily_generation_report` (report date 06-Apr-2025, image 39): 33 stations × 8 columns `[daily_gen, monthly_gen, monthly_target, fy_target, fy_gen, last_fy_gen, wy_gen, last_wy_gen]`; totals (94.1639 MU that day; 669.6931 month-to-date); grid (peak 13,264 MW, freq 49.94–50.44 Hz, state consumption 267.0128 MU, import 178.1458); dam details for Linganamakki/Supa/Mani; dashboard category totals; coal/HFO/LDO by station.
- `rtps_daily_report_2025_04_06`: per-unit gen/aux/peak/running hrs/coal/H2 cylinders for U2, U3, U5–U8; U1 and U4 have `null` (both force-tripped); station 26.245 MU, aux 2.912, PLF 63.58 %, highest-ever 41.892 MU on 2016-10-26; back-down reasons totals.
- `sgs_daily_report_2025_04_06`: 10 units at 103.5 MW each, station 14.177 MU, net export 14.078.
- `rtps_mpr_march_2025` and `rtps_mpr_fy_2024_25` (station total 831.212 MU and 8,310.351 MU): full MPR unit columns (PLF, PAF, NLD, coal, HFO, GCV, heat rate, cumulative gen/hours/trips…).
- `sgs_mpr_march_2025`: 10 units incl. commissioning dates 1964–1977; station 526.981 MU, water let out 17,564.27 MCFT.
- `alamatti_jmr_march_2025`, `rtps_jmr_april_2025`, `sale_of_energy_bill_march_2025`: JMR and bill headline numbers (bill no. 22042025; ESCOM share percentages per pool; hydel total 1,385,377,863 kWh; grand total 29,651,460,303 kWh).
- `conflicts_resolved_by_headline_total`: 4 places where the document contradicts itself; rule "printed total wins, adjust the breakdown".

### 8.2 What `reconcile_to_document.py` actually does

`main()` runs only Steps 1–3:
1. **Step 1** – overwrite `unit_daily_generation` for **2025-04-06** with the RTPS daily-report unit figures (gross, aux, peak-hour gen, peak load, running hrs, forced-outage hrs = 24 − running, coal, H2 cylinders) and SGS unit gross.
2. **Step 2** – RTPS March-2025 per-unit gross rescaled multiplicatively so the month equals the MPR; then FY 2024-25 non-March days rescaled so the FY equals the MPR (March held fixed). Aux, coal, HFO, peak-hour scale with it; `declared_capacity_mu` is lifted with `GREATEST` so PAF ≥ PLF.
3. **Step 3** – SGS March-2025 per-unit gross rescaled.
4. Steps 4 and 5 described in the docstring are **not executed**: Step 4 (HO FY/WY rescale) was deliberately removed because HO "FY gen" columns are April-to-date, not full FY (comment in `main()`); Step 5 (coal, dam, grid point values) was never implemented. The `step4_ho_station_fy_wy` function is still in the file as dead code.

The recorded run (`reconcile_log.txt`) shows every rescale factor **×1.0000** for March and FY: the workbook already matched the MPR to the decimal, so only Step 1 changes data. Step 1 is what moves e.g. RTPS-U2 on 2025-04-06 from 3.875 (workbook) to 4.008 MU.

### 8.3 `verify_reconciliation.py`

Queries the DB fresh (does not trust the log) and checks: RTPS/SGS unit and station gross for 06-Apr-2025; RTPS March and FY unit/station totals; RTPS-U1 FY = 0; SGS March units/station; 22 hard-coded workbook FY 2024-25 station totals (tolerance 0.5 MU); and four schema sanity counts (hours sum to 24, aux ≤ gross, declared ≥ gross, no negatives). Tolerance 0.01 MU elsewhere. Recorded result: **71 PASS, 0 FAIL, 0 SKIP**.

## 9. Known issues and gaps (verified, ordered by impact)

1. **Hydro categories disagree with the HO dashboard.** The DB takes `plant_category` from workbook `projects!E`, giving 14 MAJOR HYDRO stations. The document's dashboard (image 30 and anchors note) shows MAJOR HYDRO = SGS + NPH + VUGPH only (26.254 MU on 06-Apr = 14.177 + 9.289 + 2.788) and MINOR HYDRO = the other 18 stations (8.7729). `dim_station`'s comment claims the labels "match the HO dashboard groups exactly"; for hydro they do not. Any "by category" question will not reproduce the dashboard. The workbook README lists the major/minor split as an "approximate input to confirm with KPCL".
2. **Only part of the document is reproduced in the DB.** Reconciled: RTPS + SGS on 06-Apr-2025, RTPS March and FY 2024-25, SGS March, plus FY 2024-25 for other stations from the workbook's own anchors. **Not reconciled**: the other 31 stations' 06-Apr daily figures (recomputed: only 12 of 33 stations are within 0.01 MU in the raw workbook — mostly near-zero stations — e.g. BTPS 17.877 vs report 15.373, YTPS 9.831 vs 12.220, YCCPP 2.604 vs 5.030, KADRA 2.100 vs 0.750; after Step 1 RTPS and SGS also match), April month-to-date (RTPS Apr 1–6 = 139.8 in workbook vs 168.661 in the report), grid row (state generation 99.59 vs 88.87; import 167.42 vs 178.15; consumption and peak do match), RTPS/BTPS/YTPS coal stock (RTPS OB 379,633 vs report 317,409; receipt 7,992 vs 26,656), dam levels (Linganamakki 1781.36 vs 1781.4, close), the whole sale-of-energy bill and the JMR anchors.
3. **Step 1 makes daily coal inconsistent.** After Step 1, RTPS unit coal on 2025-04-06 sums to 20,572 MT (report value) but `station_coal_stock.total_consumed_mt` for that day is still the workbook's 18,470. The workbook's validation "Station coal = sum of unit coal" no longer holds for that day. Also Step 1 does not touch `hfo_consumed_kl`, GCV, heat rate, SGS aux, or `station_coal_stock`.
4. **Half the schema is empty, but the semantic layer advertises it.** Billing, commercial, JMR, DMS, org and hourly tables are empty, so `fact_jmr_meter_monthly`, `fact_energy_bill_escom`, `fact_commercial_transaction`, `fact_defect` (and the corresponding synonyms `JMR`, `sale of energy bill`) return zero rows. `station_daily_consumption` is empty even though it is a generation table: `load_gms.py` never fills it (the workbook has no such sheet), so `common_aux_mu` is always 0 and station net export equals Σ unit net export. The bill data exists only as headline numbers in the anchors JSON.
5. **`kerc_approved_mu` is 100 % null**, `is_holiday` is 100 % false, `unit_hourly_reading`/`peak_hour_gen_morning_mu`/`peak_hour_gen_evening_mu`/`peak_load_time`/`remarks` are unpopulated, so `v_unit_monthly`'s morning/evening peak-hour columns are NULL.
6. **Second workbook not integrated** and internally inconsistent with the first (5.2).
7. **Stale references.** `gms_mart.sql` header says the prompt should be built from `gms_semantic_layer.sql`; the file is `gms_semantic_layer.json`. `reconcile_to_document.py`'s module docstring still describes Steps 4–5 as active. The synonym list says "CESCOM"; the bill in the document uses "CESCORP".
8. **Ratio outliers** (5.1) — weighted formulas only.
9. **Row counts in view comments are hard-coded** (365,132; 5,877; 185,988; 22,300; 103 units; 33 stations). The first four match the workbook; they will go stale if data changes.
10. **Hard-coded paths/DSN**: scripts use CWD-relative filenames and a password-less localhost DSN; the folder is nested (`KPCL POC\KPCL POC`), so run from the inner folder.
11. `gms_mart` is plain views; the roll-ups (`fact_station_generation_monthly`) aggregate 365k rows on every call. Promote to materialised views if the chatbot is slow.
12. Not re-run here: no PostgreSQL available (see top note).

## 10. `load_gms.py` in detail (what each sheet becomes)

Reads 12 sheets: `projects, units, calendar, reservoirs, daily_unit_generation, unit_outages, generation_loss, generation_targets, coal_stock, coal_receipts, reservoir_levels, daily_grid_summary`. Uses `COPY ... FROM STDIN` per table; whole-number floats are cast to `Int64` so integer columns get `154` not `154.0`.

Hard-coded lookups in the script: 3 companies (KPCL, RPCL = Raichur Power Corporation, KPCGPCL = KPCL Gas Power Corporation); 6 plant types; 7 plant categories with the dashboard labels and order (note WTE is labelled **RES**); 6 fuels; 9 back-down reasons (QUALITY_COAL, COAL_STOCK_BUILD, SHEAR_PIN_FAILURE, UNIT_TRIP_LOSS, UNIT_STARTUP_LOSS, EQUIP_SYS_CONSTRAINT, ANNUAL_MAINTENANCE, NO_LOAD_DEMAND, FLY_ASH_SLAG); 4 coal suppliers (MCL, SCCL, WCL, IMPORTED [is_imported true]); outage reason→plant-type map for six reasons (others stay NULL = all types).

Sheet → table, with transformations:
- `projects` → `station` (`project_code`→`station_code`, `plant_category`→`plant_category_code`, dates to date; `district` not filled).
- `units` → `unit` (`fuel_type`→`fuel_code`; opening-balance columns pass through, NULL for non-RTPS).
- `calendar` → `dim_date` (`quarter`→`fy_quarter`, adds `year_month` = `YYYY-MM`, `is_holiday` NaN→false).
- `reservoirs` → `reservoir`, and `feeds_projects` (comma list) is split/exploded into `reservoir_station`.
- `daily_unit_generation` → `unit_daily_generation`: `no_of_trips`→`trips`; `declared_capacity_mu` = `paf_pct/100 × capacity_mw × 24 / 1000` (4 dp); `deemed_gen_mu` = sum of that unit-day's NO_LOAD_DEMAND `loss_mu` (else 0); GCV and heat-rate values ≤ 0 become NULL. Workbook columns `plf_pct`, `paf_pct`, `net_export_mu`, `sp_coal_kg_kwh`, `sp_oil_ml_kwh`, `capacity_mw`, `project_code` are **not loaded** (recomputed by views/generated columns).
- `generation_loss` → `unit_generation_loss`.
- `unit_outages` → `outage_reason` (distinct reason codes; description = title-cased code), `unit_outage` (`trip_datetime`→`trip_at`; `outage_hrs` NULL when `sync_datetime` is NULL), `btf_failure_type`/`btf_location` (distinct values; code = upper-cased, dash/space → `_`, e.g. `EROSION - SOOT BLOWER` → `EROSION_SOOT_BLOWER`) and `boiler_tube_failure` (`btf_id` = `outage_id`).
- `generation_targets` → `generation_target`; `coal_receipts` → `coal_receipt`; `coal_stock` → `station_coal_stock` (generated closing balance columns are not supplied); `reservoir_levels` → `reservoir_daily_level`; `daily_grid_summary` → `state_grid_daily` (only 9 of its columns).
- Finishes with `COMMIT` and `ANALYZE`.

## 11. `build_semantic_layer.py` and the question bank

The script reads `gms_mart` table/view comments and column comments from `pg_class`/`pg_attribute` (numeric precision is stripped from types), then adds three hard-coded blocks:
- **METRICS (14)**: PLF, PAF, PLF_with_NLD, aux_pct, specific_coal, specific_oil, weighted_avg_gcv_coal, weighted_avg_heat_rate, net_export_station, coal_stock_below_norm_days, kpcl_share_of_state_demand, outage_hours_by_reason, backdown_loss_by_reason, yoy_growth_pct — each with formula over mart columns and notes (e.g. "Never AVG a per-row percentage").
- **SYNONYMS (19)**: project→station, MU, PLF, PAF, NLD, back-down, trip, RSD (reserve shut down), BTF/BTL, JMR, MPR, water year, FY, major/minor hydro, RES (= waste-to-energy BWTEP), CEA target, declared capacity, ESCOM, sale of energy bill.
- **join_rules (4)** and a **units_glossary** by column suffix.
It then parses `nl2sql_question_bank.sql` with the regex `-- Q: (.*?)\n(.*?;)` into few-shot `examples` (24). Output line printed: tables/views, metrics, synonyms, examples. The intent: load `gms_semantic_layer.json`, not the `.sql` files, into the chatbot's prompt/retrieval index; regenerate it whenever comments change.

Question bank sections: HO dashboard/daily report (4), station daily report (2), MPR (4), back-down (1), outages/trips/BTF (4), coal (3), reservoirs (2), state grid (1), trends (3). One doubles as a check ("RTPS totals … 8310.351 and 831.212 MU").

## 12. Domain glossary

MU = million kWh · MW = megawatt · PLF = Plant Load Factor · PAF = Plant Availability Factor · DC = declared capacity · NLD = no-load demand (deemed generation) · RSD = reserve shut down · BTF/BTL = boiler tube failure/leak · GCV = gross calorific value (kcal/kg) · JMR = Joint Meter Reading (signed monthly billing statement, "Annexure-I") · MPR = Monthly Performance Report · HO = Head Office · UCB = unit control board (hourly log) · FRL / MDDL = full reservoir level / minimum draw-down level · WY = water year (Jun–May) · FY = Apr–Mar · ESCOM = distribution companies (BESCOM, MESCOM, CESC/CESCORP, HESCOM, GESCOM) · KPTCL = transmission company · SECI = Solar Energy Corporation of India · GoK = Government of Karnataka · K-eRP = KPCL's ERP · DMS = Defect Management System · PTW = permit to work · RTPS/BTPS/YTPS = Raichur/Bellary/Yeramarus thermal · YCCPP = Yelahanka combined-cycle · BWTEP = Bidadi waste-to-energy · SGS = Sharavathy · NPH = Nagjhari · VUGPH = Varahi underground.

## 13. Suggested next steps

1. Decide the hydro major/minor split with KPCL and fix `projects!E` (or override in `load_gms.py`), then regenerate.
2. Either implement Step 5 (coal, grid, dam point anchors; the 31 other stations' 06-Apr figures) or state clearly in the chatbot's system prompt which dates are real-anchored.
3. Populate or hide the empty facts; if hidden, drop them from `gms_semantic_layer.json` so the chatbot does not promise JMR/billing answers.
4. Fill `station_daily_consumption` (common aux) so station net export matches the reports.
5. Run the 24 reference queries in `nl2sql_question_bank.sql` against a live DB as a regression suite and compare with expected numbers.
6. ~~Build the chatbot itself.~~ Done. See Part A.

---

# Appendices — cell-level reference

Cell addresses are Excel addresses (column letter + row). "Full cell dump" tables list every non-empty cell; the row number is the first column and the Excel column letters are the header.


## Appendix A — `KPCL_GMS_10yr.xlsx`, sheet by sheet, cell by cell


#### `README` — full cell dump (49r x 1c)

| Row | A |
|---|---|
| 1 | KPCL GMS - 10-year synthetic dataset |
| 2 | SYNTHETIC DATA FOR POC - built on real anchor values from the K-eRP document; not actual KPCL operating data. |
| 4 | Scope: all 33 stations, 103 units (thermal, gas, WtE, hydro, wind, solar). Period: 01-Apr-2015 to 30-Apr-2025 (FY 2015-16 to FY 2024-25 + April 2025). |
| 5 | Tables follow gms_schema.sql (PostgreSQL design). Sheet names = table names; summary sheets are materialised views. |
| 7 | How the values were built |
| 8 | 1. Anchor year FY 2024-25 = real values from the document (see sheet 'anchors'): RTPS unit-wise FY and March-2025 figures (Monthly Performance Report), |
| 9 |    SGS unit-wise FY and March-2025 (Monthly Progress Report), BTPS March-2025 gross/net (F&A dashboard, energy bill), March-2025 billed energy for other stations, |
| 10 |    and FY 2024-25 estimates for the rest from the HO Daily Generation Report (water-year and FY-to-date figures). |
| 11 | 2. Rule: every earlier year is within 1%-5% of the following year (change drawn from 1.2%-4.8%). All units of a station move in the same direction in a year, |
| 12 |    so station totals also move 1.2%-4.8%. Same-month values follow the same rule. April 2025 follows April 2024 by the same rule. |
| 13 | 3. The rule also drives auxiliary %, specific coal, coal GCV, specific oil, CEA targets, dam storage and state demand (demand grows 1.2%-4.8% per year). |
| 14 | 4. Days are shaped by season, weekday demand and outages; planned maintenance falls in the same month each year; forced trips are short and made up within the month. |
| 15 | 5. Only documented or structural events are exempt from the rule (sheet 'exceptions'): commissioning years, RTPS U1 shut since 10-08-2022, RTPS U4 since 09-11-2024, |
| 16 |    BTPS U3 long boiler tube failures in Jan-2024, April-2025 unit outages seen in the HO report, Sirwar shutdown, and two mini hydels under 0.5 MW (Kalmala, Ganekal) whose output is below the 0.001 MU reporting resolution. |
| 18 | Result (sheet 'validation') |
| 19 |    PASS   Station annual YoY within 1-5% (non-exempt)  -  239/239 station-years in band; min 1.18% max 4.81% |
| 20 |    PASS   Unit annual YoY within 1-5% (non-exempt)  -  856/856 unit-years in band |
| 21 |    PASS   Monthly station YoY (same month) within 1-5%  -  94.0% in 1-5%; 95.6% <= 5%; median 3.13%; 95th pct 4.90% |
| 22 |    PASS   RTPS FY 2024-25 unit gross / coal / HFO = MPR  -  station 8310.351 MU (MPR 8310.351) |
| 23 |    PASS   RTPS MAR-2025 gross = 831.212 (MPR)  -  831.212 |
| 24 |    PASS   RTPS-U1 zero since 10-08-2022  -   |
| 25 |    PASS   SGS FY 2024-25 = 4967.762 (SGS MPR)  -  4967.762 |
| 26 |    PASS   SGS MAR-2025 gross 526.981 / net 523.084  -  526.981 / 523.084 |
| 27 |    PASS   BTPS MAR-2025 gross 895.327 (F&A)  -  895.327, net 836.197 |
| 28 |    PASS   SGS unit FY 2024-25 = MPR 'Cum. Gen'  -  [517.13, 458.508, 484.674, 500.349] |
| 29 |    PASS   Hours add to 24 on every unit-day  -  max dev 0.000 |
| 30 |    PASS   net = gross - aux  -   |
| 31 |    PASS   PLF = gross / (cap x 24)  -   |
| 32 |    PASS   Gross <= capacity x 24  -  max PLF 100.0 |
| 33 |    PASS   No negative values  -   |
| 34 |    PASS   Back-down reasons add up to shortfall  -  max diff 0.0000 |
| 35 |    PASS   Coal balance closes  -  days of stock 12.2-76.5 |
| 36 |    PASS   Station coal = sum of unit coal  -   |
| 37 |    PASS   No rows before commissioning  -   |
| 38 |    PASS   Full load into gms_schema.sql - all CHECK / FK rules pass  -  365132 daily rows; KPCL FY24-25 37602.2 MU; units not running at end: [('RTPS-U1', 'FORCED_OUTAGE', datetime.datetime(2022, 8, 10, 9, 0)), ('RTPS-U4', 'FORCED_OUTAGE', datetime.datetime(2024, 11, 9, 15, 58)), ('SIRWAR-U1', 'FORCED_OUTAGE', datetime.datetime(2023, 6, 1, 0, 0))] |
| 40 | Sheets |
| 41 |    YoY_check - station x FY gross generation with year-on-year % (formulas); green = within 1-5% |
| 42 |    anchors / exceptions / validation - provenance and checks |
| 43 |    annual_station_summary, annual_unit_summary, monthly_station_summary, monthly_unit_summary - roll-ups of the daily table |
| 44 |    daily_unit_generation - 365,132 rows, one per unit per day (RTPS report parameters) |
| 45 |    unit_outages, generation_loss (thermal back-down reasons), generation_targets, coal_stock, coal_receipts, reservoirs, reservoir_levels, daily_grid_summary |
| 46 |    projects, units, calendar - masters |
| 48 | Units: MU = million kWh; MT = tonnes; KL = kilolitres; hours are decimal. Percent columns hold 0-100 values (as in the database). |
| 49 | Approximate inputs to confirm with KPCL: unit commissioning dates of YTPS/BTPS-U3/solar/YCCPP, dam FRL/MDDL, hydro major/minor split, unit capacities of some hydro stations. |

#### `validation` — full cell dump (21r x 3c)

| Row | A | B | C |
|---|---|---|---|
| 1 | check | result | detail |
| 2 | Station annual YoY within 1-5% (non-exempt) | PASS | 239/239 station-years in band; min 1.18% max 4.81% |
| 3 | Unit annual YoY within 1-5% (non-exempt) | PASS | 856/856 unit-years in band |
| 4 | Monthly station YoY (same month) within 1-5% | PASS | 94.0% in 1-5%; 95.6% <= 5%; median 3.13%; 95th pct 4.90% |
| 5 | RTPS FY 2024-25 unit gross / coal / HFO = MPR | PASS | station 8310.351 MU (MPR 8310.351) |
| 6 | RTPS MAR-2025 gross = 831.212 (MPR) | PASS | 831.212 |
| 7 | RTPS-U1 zero since 10-08-2022 | PASS |  |
| 8 | SGS FY 2024-25 = 4967.762 (SGS MPR) | PASS | 4967.762 |
| 9 | SGS MAR-2025 gross 526.981 / net 523.084 | PASS | 526.981 / 523.084 |
| 10 | BTPS MAR-2025 gross 895.327 (F&A) | PASS | 895.327, net 836.197 |
| 11 | SGS unit FY 2024-25 = MPR 'Cum. Gen' | PASS | [517.13, 458.508, 484.674, 500.349] |
| 12 | Hours add to 24 on every unit-day | PASS | max dev 0.000 |
| 13 | net = gross - aux | PASS |  |
| 14 | PLF = gross / (cap x 24) | PASS |  |
| 15 | Gross <= capacity x 24 | PASS | max PLF 100.0 |
| 16 | No negative values | PASS |  |
| 17 | Back-down reasons add up to shortfall | PASS | max diff 0.0000 |
| 18 | Coal balance closes | PASS | days of stock 12.2-76.5 |
| 19 | Station coal = sum of unit coal | PASS |  |
| 20 | No rows before commissioning | PASS |  |
| 21 | Full load into gms_schema.sql - all CHECK / FK rules pass | PASS | 365132 daily rows; KPCL FY24-25 37602.2 MU; units not running at end: [('RTPS-U1', 'FORCED_OUTAGE', datetime.datetime(2022, 8, 10, 9, 0)), ('RTPS-U4', 'FORCED_OUTAGE', datetime.datetime(2024, 11, 9, 15, 58)), ('SIRWAR-U1', 'FORCED_OUTAGE', datetime.datetime(2023, 6, 1, 0, 0))] |

#### `anchors` — full cell dump (52r x 4c)

| Row | A | B | C | D |
|---|---|---|---|---|
| 1 | project_code | value | real_value | document_source |
| 2 | RTPS | FY 2024-25 gross (sum of units) | 8310.351 | RTPS Monthly Performance Report (FY cumulative) |
| 3 | RTPS | MAR-2025 gross | 831.212 | RTPS Monthly Performance Report |
| 4 | BTPS | FY 2024-25 gross (estimated) | 8181.451 | HO report 06-04-2025: WY 6878.909 - FYtd 133.195 + LastFYtd 141.22 x 61/6 |
| 5 | BTPS | MAR-2025 gross / net | 895.327 / 836.197 | F&A dashboard; energy bill unit split |
| 6 | YTPS | FY 2024-25 gross (estimated) | 6015.925 | HO report 06-04-2025: WY 4754.429 - FYtd 84.733 + LastFYtd 132.416 x 61/6 |
| 7 | YCCPP | FY 2024-25 gross (estimated) | 707.024 | HO report 06-04-2025: WY 740.498 - FYtd 33.474 + LastFYtd 0.0 x 61/6 |
| 8 | BWTEP | FY 2024-25 gross (estimated) | 6.74 | HO report 06-04-2025: WY 7.187 - FYtd 0.447 + LastFYtd 0.0 x 61/6 |
| 9 | SGS | FY 2024-25 gross (sum of units) | 4967.762 | SGS Monthly Progress Report 'Cum. Gen' |
| 10 | SGS | MAR-2025 gross | 526.981 | SGS Monthly Progress Report |
| 11 | NPH | FY 2024-25 gross (estimated) | 4096.355 | HO report 06-04-2025: WY 3577.856 - FYtd 75.377 + LastFYtd 58.414 x 61/6 |
| 12 | VUGPH | FY 2024-25 gross (estimated) | 1207.555 | HO report 06-04-2025: WY 1093.813 - FYtd 20.356 + LastFYtd 13.19 x 61/6 |
| 13 | MGHE | FY 2024-25 gross (estimated) | 443.609 | HO report 06-04-2025: WY 414.801 - FYtd 8.788 + LastFYtd 3.698 x 61/6 |
| 14 | MGHE | MAR-2025 net (billed) | 49.357 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 15 | LPH | FY 2024-25 gross (estimated) | 178.93 | HO report 06-04-2025: WY 166.62 - FYtd 2.827 + LastFYtd 1.4889 x 61/6 |
| 16 | LPH | MAR-2025 net (billed) | 15.881 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 17 | GERUSOPPA | FY 2024-25 gross (estimated) | 583.843 | HO report 06-04-2025: WY 534.3 - FYtd 10.369 + LastFYtd 5.893 x 61/6 |
| 18 | GERUSOPPA | MAR-2025 net (billed) | 49.58 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 19 | SUPA | FY 2024-25 gross (estimated) | 646.78 | HO report 06-04-2025: WY 571.9 - FYtd 12.797 + LastFYtd 8.624 x 61/6 |
| 20 | KODASALLI | FY 2024-25 gross (estimated) | 526.875 | HO report 06-04-2025: WY 459.575 - FYtd 6.85 + LastFYtd 7.2934 x 61/6 |
| 21 | KODASALLI | MAR-2025 net (billed) | 41.324 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 22 | KADRA | FY 2024-25 gross (estimated) | 532.268 | HO report 06-04-2025: WY 456.711 - FYtd 5.288 + LastFYtd 7.952 x 61/6 |
| 23 | KADRA | MAR-2025 net (billed) | 30.671 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 24 | MANI | FY 2024-25 gross (estimated) | 28.169 | HO report 06-04-2025: WY 26.79 - FYtd 0.715 + LastFYtd 0.206 x 61/6 |
| 25 | ALAMATTI | FY 2024-25 gross (estimated) | 630.111 | HO report 06-04-2025: WY 619.083 - FYtd 0.227 + LastFYtd 1.107 x 61/6 |
| 26 | ALAMATTI | MAR-2025 net (billed) | 11.939 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 27 | GHATPRABHA | FY 2024-25 gross (estimated) | 114.051 | HO report 06-04-2025: WY 84.286 - FYtd 3.578 + LastFYtd 3.2796 x 61/6 |
| 28 | GHATPRABHA | MAR-2025 net (billed) | 3.348 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 29 | BHADRA | FY 2024-25 gross (estimated) | 70.34 | HO report 06-04-2025: WY 59.438 - FYtd 2.671 + LastFYtd 1.335 x 61/6 |
| 30 | BHADRA | MAR-2025 net (billed) | 2.855 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 31 | MUNIRABAD | FY 2024-25 gross (estimated) | 122.455 | HO report 06-04-2025: WY 122.53 - FYtd 0.075 + LastFYtd 0.0 x 61/6 |
| 32 | MUNIRABAD | MAR-2025 net (billed) | 4.646 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 33 | SHIVA | FY 2024-25 gross (estimated) | 107.835 | HO report 06-04-2025: WY 108.876 - FYtd 1.047 + LastFYtd 0.0006 x 61/6 |
| 34 | SHIVA | MAR-2025 net (billed) | 10.475 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 35 | SHIMSHA | FY 2024-25 gross (estimated) | 31.104 | HO report 06-04-2025: WY 31.22 - FYtd 0.116 + LastFYtd 0.0 x 61/6 |
| 36 | SHIMSHA | MAR-2025 net (billed) | 4.067 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 37 | MALLAPUR | FY 2024-25 gross (estimated) | 44.542 | HO report 06-04-2025: WY 44.542 - FYtd 0.0 + LastFYtd 0.0 x 61/6 |
| 38 | KALMALA | FY 2024-25 gross (estimated) | 0.017 | HO report 06-04-2025: WY 0.017 - FYtd 0.0 + LastFYtd 0.0 x 61/6 |
| 39 | GANEKAL | FY 2024-25 gross (estimated) | 0.019 | HO report 06-04-2025: WY 0.019 - FYtd 0.0 + LastFYtd 0.0 x 61/6 |
| 40 | MALAPRABHA | FY 2024-25 gross (estimated) | 5.216 | HO report 06-04-2025: WY 5.216 - FYtd 0.0 + LastFYtd 0.0 x 61/6 |
| 41 | MALAPRABHA | MAR-2025 net (billed) | 0.00386 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 42 | KG-WEF | FY 2024-25 gross (estimated) | 5.458 | HO report 06-04-2025: WY 5.231 - FYtd 0.029 + LastFYtd 0.0252 x 61/6 |
| 43 | KG-WEF | MAR-2025 net (billed) | 0.197 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 44 | YAL-SOLAR-3 | FY 2024-25 gross (estimated) | 3.6 | HO report 06-04-2025: WY 2.984 - FYtd 0.058 + LastFYtd 0.0663 x 61/6 |
| 45 | YAL-SOLAR-3 | MAR-2025 net (billed) | 0.351 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 46 | ITN-SOLAR-3 | FY 2024-25 gross (estimated) | 1.563 | HO report 06-04-2025: WY 1.292 - FYtd 0.028 + LastFYtd 0.0294 x 61/6 |
| 47 | ITN-SOLAR-3 | MAR-2025 net (billed) | 0.159 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 48 | YAP-SOLAR-3 | FY 2024-25 gross (estimated) | 2.11 | HO report 06-04-2025: WY 1.788 - FYtd 0.03 + LastFYtd 0.0346 x 61/6 |
| 49 | YAP-SOLAR-3 | MAR-2025 net (billed) | 0.2 | Bill for Sale of Energy to ESCOMs, MAR-2025 |
| 50 | SH-SOLAR-5 | FY 2024-25 gross (estimated) | 4.312 | HO report 06-04-2025: WY 3.632 - FYtd 0.063 + LastFYtd 0.0731 x 61/6 |
| 51 | SH-SOLAR-10 | FY 2024-25 gross (estimated) | 11.518 | HO report 06-04-2025: WY 9.349 - FYtd 0.176 + LastFYtd 0.2307 x 61/6 |
| 52 | CHANDAPUR | FY 2024-25 gross (estimated) | 14.296 | HO report 06-04-2025: WY 11.996 - FYtd 0.242 + LastFYtd 0.25 x 61/6 |

#### `exceptions` — full cell dump (19r x 5c)

| Row | A | B | C | D | E |
|---|---|---|---|---|---|
| 1 | scope | unit_or_station | period | reason | source |
| 2 | unit | RTPS-U1 | 10-08-2022 onwards | Force tripped since 10-08-22 09:00 (RTPS daily report unit status) | Document |
| 3 | unit | SIRWAR-U1 | 01-06-2023 onwards | No generation in water years 2023-24 and 2024-25 (HO report); shutdown start assumed | Document + assumption |
| 4 | unit | BTPS-U3 | 08-01-2024 to 15-01-2024 | BTF steam cooled wall back-pass tube (BTF register) | Document |
| 5 | unit | BTPS-U3 | 20-01-2024 to 24-01-2024 | BTF final re-heater outlet tube (BTF register) | Document |
| 6 | unit | RTPS-U4 | 09-11-2024 onwards | Force tripped since 09-11-24 15:58 (RTPS daily report unit status) | Document |
| 7 | unit | YTPS-U2 | 04-04-2025 to 12-04-2025 | U2 at 0 MU on 06-04-2025 in HO report; outage window assumed | Document + assumption |
| 8 | unit | BTPS-U3 | 05-04-2025 to 18-04-2025 | U3 at 0 MU on 06-04-2025 in HO report; outage window assumed | Document + assumption |
| 9 | unit | BTPS-U3 | commissioned 01-03-2016 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 10 | unit | YTPS-U1 | commissioned 29-03-2017 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 11 | unit | YTPS-U2 | commissioned 01-03-2018 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 12 | unit | YCCPP-U1 | commissioned 01-07-2024 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 13 | unit | YCCPP-U2 | commissioned 01-07-2024 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 14 | unit | BWTEP-U1 | commissioned 01-06-2024 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 15 | unit | SH-SOLAR-5-U1 | commissioned 01-01-2017 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 16 | unit | SH-SOLAR-10-U1 | commissioned 01-01-2017 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 17 | unit | CHANDAPUR-U1 | commissioned 01-03-2017 | No data before commissioning; commissioning FY is partial | Approximate COD - confirm with KPCL |
| 18 | station | KALMALA | all years | Annual output about 0.02 MU (about 50 kWh/day) is below the 0.001 MU reporting resolution; one rounding step is about 5% | Measurement resolution |
| 19 | station | GANEKAL | all years | Annual output about 0.02 MU (about 50 kWh/day) is below the 0.001 MU reporting resolution; one rounding step is about 5% | Measurement resolution |

#### `projects` — full cell dump (34r x 9c)

| Row | A | B | C | D | E | F | G | H | I |
|---|---|---|---|---|---|---|---|---|---|
| 1 | project_code | project_name | company_code | plant_type | plant_category | installed_capacity_mw | commissioning_date | report_order | is_active |
| 2 | RTPS | Raichur Thermal Power Station | KPCL | THERMAL | THERMAL-COAL | 1720 | 1985-03-29 00:00:00 | 1 | True |
| 3 | BTPS | Bellary Thermal Power Station | KPCL | THERMAL | THERMAL-COAL | 1700 | 2008-03-01 00:00:00 | 2 | True |
| 4 | YTPS | Yeramarus Thermal Power Station | RPCL | THERMAL | THERMAL-COAL | 1600 | 2017-03-29 00:00:00 | 3 | True |
| 5 | YCCPP | Yelahanka Combined Cycle Power Plant | KPCGPCL | GAS | GAS | 370 | 2024-07-01 00:00:00 | 4 | True |
| 6 | BWTEP | Bidadi Waste to Energy Plant | KPCL | WTE | WTE | 11.5 | 2024-06-01 00:00:00 | 5 | True |
| 7 | SGS | Sharavathy Generating Station | KPCL | HYDRO | MAJOR HYDRO | 1035 | 1964-11-14 00:00:00 | 6 | True |
| 8 | NPH | Nagjhari Power House | KPCL | HYDRO | MAJOR HYDRO | 900 | 1990-01-01 00:00:00 | 7 | True |
| 9 | VUGPH | Varahi Underground Power House | KPCL | HYDRO | MAJOR HYDRO | 460 | 1990-01-01 00:00:00 | 8 | True |
| 10 | MGHE | Mahatma Gandhi Hydro Electric Station | KPCL | HYDRO | MAJOR HYDRO | 139.2 | 1990-01-01 00:00:00 | 9 | True |
| 11 | LPH | Linganamakki Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 55 | 1990-01-01 00:00:00 | 10 | True |
| 12 | GERUSOPPA | Gerusoppa Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 240 | 1990-01-01 00:00:00 | 11 | True |
| 13 | SUPA | Supa Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 100 | 1990-01-01 00:00:00 | 12 | True |
| 14 | KODASALLI | Kodasalli Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 120 | 1990-01-01 00:00:00 | 13 | True |
| 15 | KADRA | Kadra Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 150 | 1990-01-01 00:00:00 | 14 | True |
| 16 | MANI | Mani Dam Power House | KPCL | HYDRO | MINOR HYDRO | 9 | 1990-01-01 00:00:00 | 15 | True |
| 17 | ALAMATTI | Alamatti Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 290 | 1990-01-01 00:00:00 | 16 | True |
| 18 | GHATPRABHA | Ghataprabha Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 32 | 1990-01-01 00:00:00 | 17 | True |
| 19 | BHADRA | Bhadra Generating Station | KPCL | HYDRO | MAJOR HYDRO | 39.2 | 1990-01-01 00:00:00 | 18 | True |
| 20 | MUNIRABAD | Munirabad Dam Power House | KPCL | HYDRO | MAJOR HYDRO | 38 | 1990-01-01 00:00:00 | 19 | True |
| 21 | SHIVA | Shiva Hydro Electric Station | KPCL | HYDRO | MAJOR HYDRO | 42 | 1990-01-01 00:00:00 | 20 | True |
| 22 | SHIMSHA | Shimsha Hydro Electric Station | KPCL | HYDRO | MINOR HYDRO | 17.2 | 1990-01-01 00:00:00 | 21 | True |
| 23 | MALLAPUR | Mallapur Mini Hydel | KPCL | HYDRO | MINOR HYDRO | 9 | 1990-01-01 00:00:00 | 22 | True |
| 24 | SIRWAR | Sirwar Mini Hydel | KPCL | HYDRO | MINOR HYDRO | 1 | 1990-01-01 00:00:00 | 23 | True |
| 25 | KALMALA | Kalmala Mini Hydel | KPCL | HYDRO | MINOR HYDRO | 0.4 | 1990-01-01 00:00:00 | 24 | True |
| 26 | GANEKAL | Ganekal Mini Hydel | KPCL | HYDRO | MINOR HYDRO | 0.35 | 1990-01-01 00:00:00 | 25 | True |
| 27 | MALAPRABHA | Malaprabha Mini Hydel | KPCL | HYDRO | MINOR HYDRO | 2.4 | 1990-01-01 00:00:00 | 26 | True |
| 28 | KG-WEF | Kappatagudda Wind Energy Farm | KPCL | WIND | WIND | 4.555 | 2004-01-01 00:00:00 | 27 | True |
| 29 | YAL-SOLAR-3 | Yalesandra Solar PV Plant | KPCL | SOLAR | SOLAR | 3 | 2012-01-01 00:00:00 | 28 | True |
| 30 | ITN-SOLAR-3 | Itnal Solar PV Plant | KPCL | SOLAR | SOLAR | 3 | 2012-06-01 00:00:00 | 29 | True |
| 31 | YAP-SOLAR-3 | Yapaldinni Solar PV Plant | KPCL | SOLAR | SOLAR | 3 | 2012-06-01 00:00:00 | 30 | True |
| 32 | SH-SOLAR-5 | Shivasamudra Solar PV Plant 5 MW | KPCL | SOLAR | SOLAR | 5 | 2017-01-01 00:00:00 | 31 | True |
| 33 | SH-SOLAR-10 | Shivasamudra Solar PV Plant 10 MW | KPCL | SOLAR | SOLAR | 10 | 2017-01-01 00:00:00 | 32 | True |
| 34 | CHANDAPUR | Chandapur Solar PV Plant | KPCL | SOLAR | SOLAR | 10 | 2017-03-01 00:00:00 | 33 | True |

#### `units` — full cell dump (104r x 13c)

| Row | A | B | C | D | E | F | G | H | I | J | K | L | M |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | unit_id | project_code | unit_no | capacity_mw | fuel_type | technology | commissioning_date | decommissioning_date | design_heat_rate_kcal_kwh | opening_cum_gen_mu | opening_cum_running_hrs | opening_cum_trips | opening_balance_date |
| 2 | RTPS-U1 | RTPS | 1 | 210 | COAL | SUBCRITICAL | 1985-03-29 00:00:00 |  | 2450 | 37287.751 | 195538.15 | 154 | 2015-03-31 00:00:00 |
| 3 | RTPS-U2 | RTPS | 2 | 210 | COAL | SUBCRITICAL | 1986-03-01 00:00:00 |  | 2450 | 37953.416 | 195438.69 | 100 | 2015-03-31 00:00:00 |
| 4 | RTPS-U3 | RTPS | 3 | 210 | COAL | SUBCRITICAL | 1991-03-01 00:00:00 |  | 2450 | 30691.523 | 158962.42 | 64 | 2015-03-31 00:00:00 |
| 5 | RTPS-U4 | RTPS | 4 | 210 | COAL | SUBCRITICAL | 1994-03-01 00:00:00 |  | 2450 | 32007.983 | 156039.09 | 119 | 2015-03-31 00:00:00 |
| 6 | RTPS-U5 | RTPS | 5 | 210 | COAL | SUBCRITICAL | 1999-01-01 00:00:00 |  | 2450 | 24655.502 | 126963.1 | 58 | 2015-03-31 00:00:00 |
| 7 | RTPS-U6 | RTPS | 6 | 210 | COAL | SUBCRITICAL | 1999-06-01 00:00:00 |  | 2450 | 24050.903 | 123535.22 | 106 | 2015-03-31 00:00:00 |
| 8 | RTPS-U7 | RTPS | 7 | 210 | COAL | SUBCRITICAL | 2002-12-01 00:00:00 |  | 2450 | 16244.112 | 75341.78 | 139 | 2015-03-31 00:00:00 |
| 9 | RTPS-U8 | RTPS | 8 | 250 | COAL | SUBCRITICAL | 2010-06-01 00:00:00 |  | 2450 | 3881.173 | 14290.45 | 174 | 2015-03-31 00:00:00 |
| 10 | BTPS-U1 | BTPS | 1 | 500 | COAL | SUBCRITICAL | 2008-03-01 00:00:00 |  | 2450 |  |  |  |  |
| 11 | BTPS-U2 | BTPS | 2 | 500 | COAL | SUBCRITICAL | 2012-03-23 00:00:00 |  | 2450 |  |  |  |  |
| 12 | BTPS-U3 | BTPS | 3 | 700 | COAL | SUPERCRITICAL | 2016-03-01 00:00:00 |  | 2260 |  |  |  |  |
| 13 | YTPS-U1 | YTPS | 1 | 800 | COAL | SUPERCRITICAL | 2017-03-29 00:00:00 |  | 2260 |  |  |  |  |
| 14 | YTPS-U2 | YTPS | 2 | 800 | COAL | SUPERCRITICAL | 2018-03-01 00:00:00 |  | 2260 |  |  |  |  |
| 15 | YCCPP-U1 | YCCPP | 1 | 240 | GAS | CCGT | 2024-07-01 00:00:00 |  |  |  |  |  |  |
| 16 | YCCPP-U2 | YCCPP | 2 | 130 | GAS | CCGT | 2024-07-01 00:00:00 |  |  |  |  |  |  |
| 17 | BWTEP-U1 | BWTEP | 1 | 11.5 | MSW | WTE | 2024-06-01 00:00:00 |  |  |  |  |  |  |
| 18 | SGS-U1 | SGS | 1 | 103.5 | WATER | FRANCIS | 1964-11-14 00:00:00 |  |  |  |  |  |  |
| 19 | SGS-U2 | SGS | 2 | 103.5 | WATER | FRANCIS | 1965-04-22 00:00:00 |  |  |  |  |  |  |
| 20 | SGS-U3 | SGS | 3 | 103.5 | WATER | FRANCIS | 1967-10-16 00:00:00 |  |  |  |  |  |  |
| 21 | SGS-U4 | SGS | 4 | 103.5 | WATER | FRANCIS | 1967-10-09 00:00:00 |  |  |  |  |  |  |
| 22 | SGS-U5 | SGS | 5 | 103.5 | WATER | FRANCIS | 1968-04-16 00:00:00 |  |  |  |  |  |  |
| 23 | SGS-U6 | SGS | 6 | 103.5 | WATER | FRANCIS | 1968-06-08 00:00:00 |  |  |  |  |  |  |
| 24 | SGS-U7 | SGS | 7 | 103.5 | WATER | FRANCIS | 1968-05-04 00:00:00 |  |  |  |  |  |  |
| 25 | SGS-U8 | SGS | 8 | 103.5 | WATER | FRANCIS | 1971-04-16 00:00:00 |  |  |  |  |  |  |
| 26 | SGS-U9 | SGS | 9 | 103.5 | WATER | FRANCIS | 1976-08-27 00:00:00 |  |  |  |  |  |  |
| 27 | SGS-U10 | SGS | 10 | 103.5 | WATER | FRANCIS | 1977-04-09 00:00:00 |  |  |  |  |  |  |
| 28 | NPH-U1 | NPH | 1 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 29 | NPH-U2 | NPH | 2 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 30 | NPH-U3 | NPH | 3 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 31 | NPH-U4 | NPH | 4 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 32 | NPH-U5 | NPH | 5 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 33 | NPH-U6 | NPH | 6 | 150 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 34 | VUGPH-U1 | VUGPH | 1 | 115 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 35 | VUGPH-U2 | VUGPH | 2 | 115 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 36 | VUGPH-U3 | VUGPH | 3 | 115 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 37 | VUGPH-U4 | VUGPH | 4 | 115 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 38 | MGHE-U1 | MGHE | 1 | 21.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 39 | MGHE-U2 | MGHE | 2 | 21.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 40 | MGHE-U3 | MGHE | 3 | 21.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 41 | MGHE-U4 | MGHE | 4 | 21.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 42 | MGHE-U5 | MGHE | 5 | 13.2 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 43 | MGHE-U6 | MGHE | 6 | 13.2 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 44 | MGHE-U7 | MGHE | 7 | 13.2 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 45 | MGHE-U8 | MGHE | 8 | 13.2 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 46 | LPH-U1 | LPH | 1 | 27.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 47 | LPH-U2 | LPH | 2 | 27.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 48 | GERUSOPPA-U1 | GERUSOPPA | 1 | 60 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 49 | GERUSOPPA-U2 | GERUSOPPA | 2 | 60 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 50 | GERUSOPPA-U3 | GERUSOPPA | 3 | 60 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 51 | GERUSOPPA-U4 | GERUSOPPA | 4 | 60 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 52 | SUPA-U1 | SUPA | 1 | 50 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 53 | SUPA-U2 | SUPA | 2 | 50 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 54 | KODASALLI-U1 | KODASALLI | 1 | 40 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 55 | KODASALLI-U2 | KODASALLI | 2 | 40 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 56 | KODASALLI-U3 | KODASALLI | 3 | 40 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 57 | KADRA-U1 | KADRA | 1 | 50 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 58 | KADRA-U2 | KADRA | 2 | 50 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 59 | KADRA-U3 | KADRA | 3 | 50 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 60 | MANI-U1 | MANI | 1 | 4.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 61 | MANI-U2 | MANI | 2 | 4.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 62 | ALAMATTI-U1 | ALAMATTI | 1 | 15 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 63 | ALAMATTI-U2 | ALAMATTI | 2 | 55 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 64 | ALAMATTI-U3 | ALAMATTI | 3 | 55 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 65 | ALAMATTI-U4 | ALAMATTI | 4 | 55 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 66 | ALAMATTI-U5 | ALAMATTI | 5 | 55 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 67 | ALAMATTI-U6 | ALAMATTI | 6 | 55 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 68 | GHATPRABHA-U1 | GHATPRABHA | 1 | 16 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 69 | GHATPRABHA-U2 | GHATPRABHA | 2 | 16 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 70 | BHADRA-U1 | BHADRA | 1 | 12 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 71 | BHADRA-U2 | BHADRA | 2 | 12 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 72 | BHADRA-U3 | BHADRA | 3 | 7.2 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 73 | BHADRA-U4 | BHADRA | 4 | 4 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 74 | BHADRA-U5 | BHADRA | 5 | 4 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 75 | MUNIRABAD-U1 | MUNIRABAD | 1 | 9 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 76 | MUNIRABAD-U2 | MUNIRABAD | 2 | 9 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 77 | MUNIRABAD-U3 | MUNIRABAD | 3 | 10 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 78 | MUNIRABAD-U4 | MUNIRABAD | 4 | 10 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 79 | SHIVA-U1 | SHIVA | 1 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 80 | SHIVA-U2 | SHIVA | 2 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 81 | SHIVA-U3 | SHIVA | 3 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 82 | SHIVA-U4 | SHIVA | 4 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 83 | SHIVA-U5 | SHIVA | 5 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 84 | SHIVA-U6 | SHIVA | 6 | 3 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 85 | SHIVA-U7 | SHIVA | 7 | 6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 86 | SHIVA-U8 | SHIVA | 8 | 6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 87 | SHIVA-U9 | SHIVA | 9 | 6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 88 | SHIVA-U10 | SHIVA | 10 | 6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 89 | SHIMSHA-U1 | SHIMSHA | 1 | 8.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 90 | SHIMSHA-U2 | SHIMSHA | 2 | 8.6 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 91 | MALLAPUR-U1 | MALLAPUR | 1 | 4.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 92 | MALLAPUR-U2 | MALLAPUR | 2 | 4.5 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 93 | SIRWAR-U1 | SIRWAR | 1 | 1 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 94 | KALMALA-U1 | KALMALA | 1 | 0.4 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 95 | GANEKAL-U1 | GANEKAL | 1 | 0.35 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 96 | MALAPRABHA-U1 | MALAPRABHA | 1 | 2.4 | WATER | FRANCIS | 1990-01-01 00:00:00 |  |  |  |  |  |  |
| 97 | KG-WEF-U1 | KG-WEF | 1 | 2 | WIND | WTG | 2004-01-01 00:00:00 |  |  |  |  |  |  |
| 98 | KG-WEF-U2 | KG-WEF | 2 | 2.555 | WIND | WTG | 2004-01-01 00:00:00 |  |  |  |  |  |  |
| 99 | YAL-SOLAR-3-U1 | YAL-SOLAR-3 | 1 | 3 | SUN | PV | 2012-01-01 00:00:00 |  |  |  |  |  |  |
| 100 | ITN-SOLAR-3-U1 | ITN-SOLAR-3 | 1 | 3 | SUN | PV | 2012-06-01 00:00:00 |  |  |  |  |  |  |
| 101 | YAP-SOLAR-3-U1 | YAP-SOLAR-3 | 1 | 3 | SUN | PV | 2012-06-01 00:00:00 |  |  |  |  |  |  |
| 102 | SH-SOLAR-5-U1 | SH-SOLAR-5 | 1 | 5 | SUN | PV | 2017-01-01 00:00:00 |  |  |  |  |  |  |
| 103 | SH-SOLAR-10-U1 | SH-SOLAR-10 | 1 | 10 | SUN | PV | 2017-01-01 00:00:00 |  |  |  |  |  |  |
| 104 | CHANDAPUR-U1 | CHANDAPUR | 1 | 10 | SUN | PV | 2017-03-01 00:00:00 |  |  |  |  |  |  |

#### `reservoirs` — full cell dump (7r x 7c)

| Row | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| 1 | reservoir_id | reservoir_name | level_unit | full_reservoir_level | min_draw_down_level | energy_at_frl_mu | feeds_projects |
| 2 | 1 | LINGANAMAKKI | FT | 1819 | 1715 | 4598.5 | SGS,LPH |
| 3 | 2 | TALAKALALE | FT | 1719 | 1640 |  | SGS |
| 4 | 3 | SUPA | M | 564 | 495 | 3159.1 | NPH,SUPA |
| 5 | 4 | MANI | M | 594.36 | 563.88 | 972.8 | VUGPH,MANI |
| 6 | 5 | GERUSOPPA | M | 55 | 45 |  | GERUSOPPA |
| 7 | 6 | ALAMATTI | M | 519.6 | 505 |  | ALAMATTI |

#### `YoY_check` — layout (39r x 21c)

- Row 1 (A1): title. Row 2 (A2): note that values are SUMIFS formulas and colour rules. Row 4: headers — A4 `project_code`, B4:K4 = FY labels `2015-16` … `2024-25`, L4 empty spacer, M4:U4 = `YoY 2016-17` … `YoY 2024-25`.
- Rows 5–37: one row per station (33 rows, same order as `projects`). B5:K37 = `=SUMIFS(annual_station_summary!$C$2:$C$309, annual_station_summary!$B$2:$B$309, $A5, annual_station_summary!$A$2:$A$309, B$4)` i.e. gross_gen_mu for (project_code=$A, fy=header). M5:U37 = year-on-year % of adjacent B:K cells (see raw formulas below).
- Conditional formatting: green = YoY within 1–5 %, grey = exempt year listed in `exceptions`.
- Raw formulas, row 5 (RTPS) M–U: M5=`=IF(B5<=0,"",C5/B5-1)` ; N5=`=IF(C5<=0,"",D5/C5-1)` ; O5=`=IF(D5<=0,"",E5/D5-1)` ; P5=`=IF(E5<=0,"",F5/E5-1)` ; Q5=`=IF(F5<=0,"",G5/F5-1)` ; R5=`=IF(G5<=0,"",H5/G5-1)` ; S5=`=IF(H5<=0,"",I5/H5-1)` ; T5=`=IF(I5<=0,"",J5/I5-1)` ; U5=`=IF(J5<=0,"",K5/J5-1)`
- Rows beyond 37 (up to row 38): [(None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None), ('KPCL total', '=SUM(B5:B37)', '=SUM(C5:C37)', '=SUM(D5:D37)', '=SUM(E5:E37)', '=SUM(F5:F37)', '=SUM(G5:G37)', '=SUM(H5:H37)', '=SUM(I5:I37)', '=SUM(J5:J37)', '=SUM(K5:K37)', None, None, None, None, None, None, None, None, None, None)]

#### `annual_station_summary` — column map (309 rows x 16 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | fy | text/bool | 0 | 10 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25 |
| B | project_code | text/bool | 0 | ALAMATTI | YTPS |
| C | gross_gen_mu | number | 0 | 0.0 | 11009.087 |
| D | aux_consumption_mu | number | 0 | 0.0 | 1175.562 |
| E | net_export_mu | number | 0 | 0.0 | 9838.677 |
| F | forced_outage_hrs | number | 0 | 0.0 | 14752.46 |
| G | planned_outage_hrs | number | 0 | 0.0 | 6993.48 |
| H | rsd_hrs | number | 0 | 0.0 | 55096.84 |
| I | trips | number | 0 | 0.0 | 123.0 |
| J | coal_consumed_mt | number | 0 | 0.0 | 8738326.0 |
| K | hfo_consumed_kl | number | 0 | 0.0 | 12524.76 |
| L | ldo_consumed_kl | number | 0 | 0.0 | 337.43 |
| M | water_used_mcft | number | 0 | 0.0 | 167032.89 |
| N | plf_pct | number | 0 | 0.0 | 80.85 |
| O | aux_pct | number | 1 | 0.0 | 18.07 |
| P | sp_coal_kg_kwh | number | 1 | 0.0 | 0.796 |

First data row (row 2): A2=2015-16; B2=ALAMATTI; C2=645.539; D2=5.716; E2=639.823; F2=109.08; G2=2024.49; H2=35720.22; I2=17; J2=0.0; K2=0.0; L2=0.0; M2=116179.71; N2=25.34; O2=0.89; P2=0.0

Last data row (row 309): A309=2024-25; B309=YTPS; C309=6015.926; D309=372.993; E309=5642.933; F309=193.8; G309=1506.35; H309=562.05; I309=12; J309=4058505.29; K309=4065.01; L309=211.94; M309=0.0; N309=42.92; O309=6.2; P309=0.675

#### `annual_unit_summary` — column map (998 rows x 17 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | fy | text/bool | 0 | 10 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25 |
| B | unit_id | text/bool | 0 | ALAMATTI-U1 | YTPS-U2 |
| C | project_code | text/bool | 0 | ALAMATTI | YTPS |
| D | gross_gen_mu | number | 0 | 0.0 | 3088.33 |
| E | aux_consumption_mu | number | 0 | 0.0 | 193.05 |
| F | net_export_mu | number | 0 | 0.0 | 2904.478 |
| G | forced_outage_hrs | number | 0 | 0.0 | 8784.0 |
| H | planned_outage_hrs | number | 0 | 0.0 | 1479.88 |
| I | rsd_hrs | number | 0 | 0.0 | 8342.03 |
| J | trips | number | 0 | 0.0 | 25.0 |
| K | coal_consumed_mt | number | 0 | 0.0 | 2134574.31 |
| L | hfo_consumed_kl | number | 0 | 0.0 | 3630.51 |
| M | ldo_consumed_kl | number | 0 | 0.0 | 160.47 |
| N | water_used_mcft | number | 0 | 0.0 | 23243.62 |
| O | plf_pct | number | 0 | 0.0 | 84.17 |
| P | aux_pct | number | 3 | 0.0 | 18.07 |
| Q | sp_coal_kg_kwh | number | 3 | 0.0 | 0.888 |

First data row (row 2): A2=2015-16; B2=ALAMATTI-U1; C2=ALAMATTI; D2=33.576; E2=0.304; F2=33.272; G2=6.21; H2=393.3; I2=5897.440000000001; J2=1; K2=0.0; L2=0.0; M2=0.0; N2=6044.68; O2=25.48; P2=0.91; Q2=0.0

Last data row (row 998): A998=2024-25; B998=YTPS-U2; C998=YTPS; D998=3059.793; E998=189.709; F998=2870.084; G998=110.08; H998=656.28; I998=281.15; J998=6; K998=2064218.58; L998=1844.66; M998=117.53; N998=0.0; O998=43.66; P998=6.2; Q998=0.675

#### `monthly_station_summary` — column map (3685 rows x 17 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | ym | text/bool | 0 | 2015-04 | 2025-04 |
| B | fy | text/bool | 0 | 11 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25, 2025-26 |
| C | project_code | text/bool | 0 | ALAMATTI | YTPS |
| D | gross_gen_mu | number | 0 | 0.0 | 1195.561 |
| E | aux_consumption_mu | number | 0 | 0.0 | 125.04 |
| F | net_export_mu | number | 0 | 0.0 | 1070.521 |
| G | forced_outage_hrs | number | 0 | 0.0 | 1871.27 |
| H | planned_outage_hrs | number | 0 | 0.0 | 2404.08 |
| I | rsd_hrs | number | 0 | 0.0 | 6991.33 |
| J | trips | number | 0 | 0.0 | 17.0 |
| K | coal_consumed_mt | number | 0 | 0.0 | 942202.04 |
| L | hfo_consumed_kl | number | 0 | 0.0 | 2694.22 |
| M | ldo_consumed_kl | number | 0 | 0.0 | 126.64 |
| N | water_used_mcft | number | 0 | 0.0 | 23853.46 |
| O | plf_pct | number | 0 | 0.0 | 98.0 |
| P | aux_pct | number | 65 | 0.0 | 18.94 |
| Q | sp_coal_kg_kwh | number | 65 | 0.0 | 0.827 |

First data row (row 2): A2=2015-04; B2=2015-16; C2=ALAMATTI; D2=5.368; E2=0.001; F2=5.367; G2=15.77; H2=1005.23; I2=3181.55; J2=2; K2=0.0; L2=0.0; M2=0.0; N2=965.38; O2=2.57; P2=0.02; Q2=0.0

Last data row (row 3685): A3685=2025-04; B3685=2025-26; C3685=YTPS; D3685=526.982; E3685=32.593; F3685=494.389; G3685=205.9; H3685=0.0; I3685=0.0; J3685=4; K3685=364563.81; L3685=384.93; M3685=8.68; N3685=0.0; O3685=45.74; P3685=6.18; Q3685=0.692

#### `monthly_unit_summary` — column map (11998 rows x 18 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | ym | text/bool | 0 | 2015-04 | 2025-04 |
| B | fy | text/bool | 0 | 11 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25, 2025-26 |
| C | unit_id | text/bool | 0 | ALAMATTI-U1 | YTPS-U2 |
| D | project_code | text/bool | 0 | ALAMATTI | YTPS |
| E | gross_gen_mu | number | 0 | 0.0 | 343.704 |
| F | aux_consumption_mu | number | 0 | 0.0 | 20.874 |
| G | net_export_mu | number | 0 | 0.0 | 323.644 |
| H | forced_outage_hrs | number | 0 | 0.0 | 744.0 |
| I | planned_outage_hrs | number | 0 | 0.0 | 744.0 |
| J | rsd_hrs | number | 0 | 0.0 | 744.0 |
| K | trips | number | 0 | 0.0 | 9.0 |
| L | coal_consumed_mt | number | 0 | 0.0 | 234644.15 |
| M | hfo_consumed_kl | number | 0 | 0.0 | 1093.04 |
| N | ldo_consumed_kl | number | 0 | 0.0 | 80.86 |
| O | water_used_mcft | number | 0 | 0.0 | 4766.47 |
| P | plf_pct | number | 0 | 0.0 | 100.0 |
| Q | aux_pct | number | 118 | 0.0 | 18.94 |
| R | sp_coal_kg_kwh | number | 118 | 0.0 | 0.91 |

First data row (row 2): A2=2015-04; B2=2015-16; C2=ALAMATTI-U1; D2=ALAMATTI; E2=0.164; F2=0.0; G2=0.164; H2=0.0; I2=393.3; J2=314.57; K2=0; L2=0.0; M2=0.0; N2=0.0; O2=29.54; P2=1.52; Q2=0.0; R2=0.0

Last data row (row 11998): A11998=2025-04; B11998=2025-26; C11998=YTPS-U2; D11998=YTPS; E11998=223.031; F11998=14.469; G11998=208.562; H11998=198.45; I11998=0.0; J11998=0.0; K11998=2; L11998=154705.37; M11998=136.4; N11998=8.68; O11998=0.0; P11998=38.72; Q11998=6.49; R11998=0.694

#### `calendar` — column map (3684 rows x 15 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | cal_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | fy | text/bool | 0 | 11 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25, 2025-26 |
| C | fy_start | date | 0 | 11 distinct | 2015-04-01 00:00:00, 2016-04-01 00:00:00, 2017-04-01 00:00:00, 2018-04-01 00:00:00, 2019-04-01 00:00:00, 2020-04-01 00:0 |
| D | fy_month_no | number | 0 | 1.0 | 12.0 |
| E | water_year | text/bool | 0 | 11 distinct | 2014-15, 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25 |
| F | wy_month_no | number | 0 | 1.0 | 12.0 |
| G | month_start | date | 0 | 2015-04-01 00:00:00 | 2025-04-01 00:00:00 |
| H | month_label | text/bool | 0 | APR-2015 | SEP-2024 |
| I | quarter | text/bool | 0 | 4 distinct | Q1, Q2, Q3, Q4 |
| J | week_start | date | 0 | 2015-03-30 00:00:00 | 2025-04-28 00:00:00 |
| K | day_name | text/bool | 0 | 7 distinct | Friday, Monday, Saturday, Sunday, Thursday, Tuesday, Wednesday |
| L | is_weekend | text/bool | 0 | 2 distinct | False, True |
| M | is_holiday | text/bool | 0 | 1 distinct | False |
| N | holiday_name | number | 3683 |  |  |
| O | season | text/bool | 0 | 4 distinct | MONSOON, POST_MONSOON, SUMMER, WINTER |

First data row (row 2): A2=2015-04-01 00:00:00; B2=2015-16; C2=2015-04-01 00:00:00; D2=1; E2=2014-15; F2=11; G2=2015-04-01 00:00:00; H2=APR-2015; I2=Q1; J2=2015-03-30 00:00:00; K2=Wednesday; L2=False; M2=False; N2=nan; O2=SUMMER

Last data row (row 3684): A3684=2025-04-30 00:00:00; B3684=2025-26; C3684=2025-04-01 00:00:00; D3684=1; E3684=2024-25; F3684=11; G3684=2025-04-01 00:00:00; H3684=APR-2025; I3684=Q1; J3684=2025-04-28 00:00:00; K3684=Wednesday; L3684=False; M3684=False; N3684=nan; O3684=SUMMER

#### `daily_unit_generation` — column map (365133 rows x 30 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | gen_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | unit_id | text/bool | 0 | ALAMATTI-U1 | YTPS-U2 |
| C | project_code | text/bool | 0 | ALAMATTI | YTPS |
| D | capacity_mw | number | 0 | 0.35 | 800.0 |
| E | gross_gen_mu | number | 0 | 0.0 | 19.2 |
| F | aux_consumption_mu | number | 0 | 0.0 | 1.175 |
| G | net_export_mu | number | 0 | 0.0 | 18.025 |
| H | peak_hour_gen_mu | number | 0 | 0.0 | 6.4 |
| I | peak_load_mw | number | 0 | 0.0 | 800.0 |
| J | running_hrs | number | 0 | 0.0 | 24.0 |
| K | forced_outage_hrs | number | 0 | 0.0 | 24.0 |
| L | planned_outage_hrs | number | 0 | 0.0 | 24.0 |
| M | rsd_hrs | number | 0 | 0.0 | 24.0 |
| N | no_of_trips | number | 0 | 0.0 | 2.0 |
| O | plf_pct | number | 0 | 0.0 | 100.0 |
| P | paf_pct | number | 0 | 0.0 | 100.0 |
| Q | coal_consumed_mt | number | 319381 | 0.0 | 13045.23 |
| R | hfo_consumed_kl | number | 319381 | 0.0 | 499.85 |
| S | ldo_consumed_kl | number | 319381 | 0.0 | 43.46 |
| T | gas_consumed_mmscm | number | 364524 | 0.0 | 0.4806 |
| U | msw_consumed_mt | number | 364798 | 45.21 | 89.74 |
| V | sp_coal_kg_kwh | number | 326020 | 0.615 | 0.951 |
| W | sp_oil_ml_kwh | number | 326020 | 0.0 | 25298.333 |
| X | gcv_coal_kcal_kg | number | 326020 | 2972.0 | 3679.0 |
| Y | gcv_oil_kcal_l | number | 326020 | 10300.0 | 10321.0 |
| Z | gross_heat_rate_kcal_kwh | number | 326020 | 2144.0 | 263995.0 |
| AA | dm_water_cum | number | 319381 | 0.0 | 1018.3 |
| AB | h2_cylinders_nos | number | 319381 | 0.0 | 10.0 |
| AC | water_used_mcft | number | 74175 | 0.0 | 195.08 |
| AD | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=2015-04-01 00:00:00; B2=RTPS-U1; C2=RTPS; D2=210.0; E2=4.382; F2=0.446; G2=3.936; H2=1.561; I2=198.1; J2=24.0; K2=0.0; L2=0.0; M2=0.0; N2=0; O2=86.94; P2=100.0; Q2=3226.81; R2=0.0; S2=0.0; T2=nan; U2=nan; V2=0.736; W2=0.0; X2=3458.0; Y2=10321.0; Z2=2546.0; AA2=74.1; AB2=0.0; AC2=nan; AD2=SYNTHETIC

Last data row (row 365133): A365133=2025-04-30 00:00:00; B365133=CHANDAPUR-U1; C365133=CHANDAPUR; D365133=10.0; E365133=0.053; F365133=0.001; G365133=0.052; H365133=0.02; I365133=7.98; J365133=12.0; K365133=0.0; L365133=0.0; M365133=12.0; N365133=0; O365133=22.08; P365133=100.0; Q365133=nan; R365133=nan; S365133=nan; T365133=nan; U365133=nan; V365133=nan; W365133=nan; X365133=nan; Y365133=nan; Z365133=nan; AA365133=nan; AB365133=nan; AC365133=nan; AD365133=SYNTHETIC

#### `unit_outages` — column map (5878 rows x 16 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | outage_id | number | 0 | 100001.0 | 105877.0 |
| B | unit_id | text/bool | 0 | ALAMATTI-U1 | YTPS-U2 |
| C | project_code | text/bool | 0 | ALAMATTI | YTPS |
| D | outage_type | text/bool | 0 | 3 distinct | FORCED, PLANNED, RSD |
| E | trip_datetime | date | 0 | 2015-04-01 19:21:00 | 2025-04-29 09:41:00 |
| F | sync_datetime | date | 3 | 2015-04-01 23:27:12.178000 | 2025-04-29 14:40:42.454000 |
| G | outage_hrs | number | 3 | 1.5 | 1479.88 |
| H | reason_code | text/bool | 0 | ANNUAL_OVERHAUL | TURBINE |
| I | reason_detail | text/bool | 0 | 107.42 h; re-synchronised 23-0 | U3 at 0 MU on 06-04-2025 in HO |
| J | is_btf | text/bool | 0 | 2 distinct | False, True |
| K | gen_loss_mu | number | 3 | 0.0005 | 680.056 |
| L | is_structural | text/bool | 0 | 2 distinct | False, True |
| M | event_source | text/bool | 0 | 3 distinct | Document, Document + assumption, Synthetic |
| N | btf_failure_type | text/bool | 5530 | 5 distinct | EROSION - FALLING SLAG, EROSION - SOOT BLOWER, OTHERS, STRESS RUPTURE - HIGH TEMPERATURE CREEP, WELD DEFECT |
| O | btf_location | text/bool | 5530 | 5 distinct | ECONOMISER, RE-HEATER, STEAM COOLED WALL, SUPER HEATER, WATER WALL |
| P | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=100001; B2=BTPS-U1; C2=BTPS; D2=FORCED; E2=2015-04-01 19:21:00; F2=2015-04-01 23:27:12.178000; G2=4.1; H2=TURBINE; I2=Turbine protection trip; J2=False; K2=2.05; L2=False; M2=Synthetic; N2=nan; O2=nan; P2=SYNTHETIC

Last data row (row 5878): A5878=105877; B5878=MGHE-U5; C5878=MGHE; D5878=FORCED; E5878=2025-04-29 09:41:00; F5878=2025-04-29 14:40:42.454000; G5878=5.0; H5878=ELECTRICAL; I5878=GT / UAT / switchyard fault; J5878=False; K5878=0.066; L5878=False; M5878=Synthetic; N5878=nan; O5878=nan; P5878=SYNTHETIC

#### `generation_loss` — column map (185989 rows x 6 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | loss_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | unit_id | text/bool | 0 | BTPS-U1 | YTPS-U2 |
| C | reason_code | text/bool | 0 | 8 distinct | ANNUAL_MAINTENANCE, EQUIP_SYS_CONSTRAINT, FLY_ASH_SLAG, NO_LOAD_DEMAND, QUALITY_COAL, SHEAR_PIN_FAILURE, UNIT_STARTUP_LO |
| D | project_code | text/bool | 0 | 4 distinct | BTPS, RTPS, YCCPP, YTPS |
| E | loss_mu | number | 0 | 0.001 | 19.2 |
| F | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=2015-04-01 00:00:00; B2=RTPS-U1; C2=EQUIP_SYS_CONSTRAINT; D2=RTPS; E2=0.211; F2=SYNTHETIC

Last data row (row 185989): A185989=2025-04-30 00:00:00; B185989=YCCPP-U2; C185989=NO_LOAD_DEMAND; D185989=YCCPP; E185989=1.266; F185989=SYNTHETIC

#### `generation_targets` — column map (11998 rows x 7 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | target_month | date | 0 | 2015-04-01 00:00:00 | 2025-04-01 00:00:00 |
| B | unit_id | text/bool | 0 | ALAMATTI-U1 | YTPS-U2 |
| C | fy | text/bool | 0 | 11 distinct | 2015-16, 2016-17, 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24, 2024-25, 2025-26 |
| D | project_code | text/bool | 0 | ALAMATTI | YTPS |
| E | cea_target_mu | number | 0 | 0.0 | 387.0 |
| F | kerc_approved_mu | number | 11997 |  |  |
| G | internal_target_mu | number | 0 | 0.0 | 406.35 |

First data row (row 2): A2=2015-04-01 00:00:00; B2=RTPS-U1; C2=2015-16; D2=RTPS; E2=138.786; F2=nan; G2=145.725

Last data row (row 11998): A11998=2025-04-01 00:00:00; B11998=CHANDAPUR-U1; C11998=2025-26; D11998=CHANDAPUR; E11998=1.35; F11998=nan; G11998=1.418

#### `coal_stock` — column map (10322 rows x 11 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | stock_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | project_code | text/bool | 0 | 3 distinct | BTPS, RTPS, YTPS |
| C | opening_stock_mt | number | 0 | 58462.17 | 562799.69 |
| D | receipt_mt | number | 0 | 0.0 | 39824.26 |
| E | indigenous_consumed_mt | number | 0 | 0.0 | 32553.49 |
| F | imported_consumed_mt | number | 0 | 0.0 | 1267.9 |
| G | total_consumed_mt | number | 0 | 0.0 | 32553.49 |
| H | closing_stock_mt | number | 0 | 58462.17 | 562799.69 |
| I | days_of_stock | number | 0 | 12.2 | 76.5 |
| J | normative_days | number | 0 | 20.0 | 20.0 |
| K | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=2015-04-01 00:00:00; B2=RTPS; C2=496443.06; D2=27441.21; E2=27536.98; F2=1147.37; G2=28684.35; H2=495199.92; I2=17.3; J2=20; K2=SYNTHETIC

Last data row (row 10322): A10322=2025-04-30 00:00:00; B10322=YTPS; C10322=256976.77; D10322=19571.28; E10322=13392.31; F10322=558.01; G10322=13950.32; H10322=262597.73; I10322=18.4; J10322=20; K10322=SYNTHETIC

#### `coal_receipts` — column map (22301 rows x 8 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | receipt_id | number | 0 | 1.0 | 22300.0 |
| B | project_code | text/bool | 0 | 3 distinct | BTPS, RTPS, YTPS |
| C | receipt_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| D | supplier_code | text/bool | 0 | 4 distinct | IMPORTED, MCL, SCCL, WCL |
| E | no_of_rakes | number | 0 | 1.0 | 9.0 |
| F | quantity_mt | number | 0 | 3800.03 | 35338.57 |
| G | gcv_received_kcal_kg | number | 0 | 2925.0 | 5769.0 |
| H | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=1; B2=RTPS; C2=2015-04-01 00:00:00; D2=IMPORTED; E2=1; F2=3831.56; G2=5070; H2=SYNTHETIC

Last data row (row 22301): A22301=22300; B22301=YTPS; C22301=2025-04-30 00:00:00; D22301=WCL; E22301=1; F22301=3815.1; G22301=3474; H22301=SYNTHETIC

#### `reservoir_levels` — column map (22099 rows x 10 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | level_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | reservoir_id | number | 0 | 1.0 | 6.0 |
| C | water_level | number | 0 | 47.76 | 1818.96 |
| D | capacity_pct | number | 0 | 4.62 | 100.0 |
| E | inflow_mcft | number | 0 | 0.0 | 3755.67 |
| F | discharge_power_mcft | number | 0 | 13.95 | 845.51 |
| G | spill_sluice_mcft | number | 0 | 0.0 | 1170.37 |
| H | rainfall_mm | number | 0 | 0.0 | 143.0 |
| I | energy_potential_mu | number | 11049 | 129.6 | 4595.03 |
| J | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=2015-04-01 00:00:00; B2=1; C2=1782.67; D2=39.18; E2=204.81; F2=341.35; G2=0.0; H2=0.0; I2=1801.62; J2=SYNTHETIC

Last data row (row 22099): A22099=2025-04-30 00:00:00; B22099=6; C22099=512.23; D22099=24.53; E22099=0.0; F22099=33.58; G22099=0.0; H22099=0.0; I22099=nan; J22099=SYNTHETIC

#### `daily_grid_summary` — column map (3684 rows x 10 cols); header on row 1, data starts row 2

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | summary_date | date | 0 | 2015-04-01 00:00:00 | 2025-04-30 00:00:00 |
| B | state_consumption_mu | number | 0 | 146.7747 | 293.9257 |
| C | state_generation_mu | number | 0 | 48.1878 | 119.5372 |
| D | state_import_mu | number | 0 | 89.6635 | 195.4623 |
| E | system_peak_mw | number | 0 | 7036.0 | 14354.0 |
| F | freq_max_hz | number | 0 | 50.05 | 50.44 |
| G | freq_min_hz | number | 0 | 49.82 | 49.97 |
| H | tb_share_mu | number | 0 | 0.0 | 0.5991 |
| I | jurala_share_mu | number | 0 | 0.0 | 0.3999 |
| J | data_source | text/bool | 0 | 1 distinct | SYNTHETIC |

First data row (row 2): A2=2015-04-01 00:00:00; B2=203.6157; C2=83.6401; D2=119.9756; E2=10110; F2=50.28; G2=49.86; H2=0.0074; I2=0.0; J2=SYNTHETIC

Last data row (row 3684): A3684=2025-04-30 00:00:00; B3684=269.2458; C3684=90.9508; D3684=178.2951; E3684=13540; F3684=50.06; G3684=49.92; H3684=0.0295; I3684=0.0; J3684=SYNTHETIC

## Appendix B — `KPCL_33_Projects_10Y_Joint_Meter_and_MPR.xlsx`, cell by cell


#### `README` — full cell dump (13r x 7c)

| Row | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| 1 | KPCL – 33 Projects – 10-Year Joint Meter Reading & Monthly Performance Reports |  |  |  |  |  |  |
| 3 | Coverage | April 2016 to March 2026 (120 months) |  |  |  |  |  |
| 4 | Projects | 33 |  |  |  |  |  |
| 5 | Joint Meter records | 3960 |  |  |  |  |  |
| 6 | MPR records | 3960 |  |  |  |  |  |
| 7 | Data type | Synthetic/generated data |  |  |  |  |  |
| 8 | Reference | Uploaded KPCL HO-Daily Generation Report / RTPS Joint Energy Meter and Monthly Performance Report structures |  |  |  |  |  |
| 11 | Important | The source report supports the report structure and terminology. The 10-year values in this workbook are synthetic and should not be represented as official KPCL readings. |  |  |  |  |  |

#### `Project_Master` — full cell dump (36r x 3c)

| Row | A | B | C |
|---|---|---|---|
| 1 | 33 projects used for the synthetic 10-year reports. |  |  |
| 3 | Project | Installed Capacity (MW) | Plant Type |
| 4 | RTPS | 1720 | Thermal |
| 5 | BTPS | 1700 | Thermal |
| 6 | YTPS | 1600 | Thermal |
| 7 | YCCPP | 370 | Combined Cycle |
| 8 | BWtEP | 11.5 | Waste to Energy |
| 9 | SGS | 1035 | Hydro |
| 10 | NPH | 900 | Hydro |
| 11 | VUGPH | 460 | Hydro |
| 12 | MGHE | 139.2 | Hydro |
| 13 | LPH | 55 | Hydro |
| 14 | GERUSOPPA | 240 | Hydro |
| 15 | SUPA | 100 | Hydro |
| 16 | KODSALLI | 120 | Hydro |
| 17 | KADRA | 150 | Hydro |
| 18 | MANI | 9 | Hydro |
| 19 | ALAMATTI | 290 | Hydro |
| 20 | GHATAPRABHA | 32.7 | Hydro |
| 21 | BHADRA | 39.2 | Hydro |
| 22 | MUNIRABAD | 38 | Hydro |
| 23 | SHIVA | 42 | Hydro |
| 24 | SHIMSHA | 17.2 | Hydro |
| 25 | MALLAPUR | 9 | Hydro |
| 26 | SIRWAR | 1.02 | Hydro |
| 27 | KALMALA | 4 | Hydro |
| 28 | GANEKAL | 35 | Hydro |
| 29 | MALPRABHA | 2.4 | Hydro |
| 30 | KG-WER | 4.555 | Renewable |
| 31 | YAL-SOLAR-3 | 3.7 | Solar |
| 32 | TNL-SOLAR-3 | 3.07 | Solar |
| 33 | YAP-SOLAR-3 | 3.07 | Solar |
| 34 | SH-SOLAR-5 | 5 | Solar |
| 35 | SH-SOLAR-10 | 10 | Solar |
| 36 | CHANDAPUR | 10 | Solar |

#### `10Y_MPR_Summary` — full cell dump (36r x 9c)

| Row | A | B | C | D | E | F | G | H | I |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 10-year project summary from the synthetic Monthly Performance Report. |  |  |  |  |  |  |  |  |
| 3 | Project | Plant Type | Installed Capacity (MW) | 10Y Gross Generation (MU) | 10Y Net Generation (MU) | Average PLF (%) | Average Availability (%) | 10Y Planned Outage Hours | 10Y Forced Outage Hours |
| 4 | ALAMATTI | Hydro | 290 | 11199.81583623662 | 10923.11974712898 | 44.01054424763725 | 93.44036193525072 | 7117.032681003408 | 2253.884651961935 |
| 5 | BHADRA | Hydro | 39.2 | 1571.330303686271 | 1533.202085673403 | 45.67715270182579 | 93.73876341323275 | 6087.619796616719 | 2750.85256450852 |
| 6 | BTPS | Thermal | 1700 | 102585.4437914868 | 93792.79172882151 | 68.86922414605036 | 90.42735713782662 | 6059.786055610067 | 2590.999167495863 |
| 7 | BWtEP | Waste to Energy | 11.5 | 664.6103701212489 | 597.7581046500637 | 65.95277976128867 | 91.07055229495708 | 6462.434935109302 | 2646.974627231568 |
| 8 | CHANDAPUR | Solar | 10 | 165.9394280553815 | 163.4265050341227 | 18.93061832515998 | 95.6661262732535 | 6049.975809587895 | 2587.441261515264 |
| 9 | GANEKAL | Hydro | 35 | 1303.962391186679 | 1271.457551501652 | 42.44847815632285 | 93.78724201766484 | 6764.426481033684 | 2794.811926132523 |
| 10 | GERUSOPPA | Hydro | 240 | 9013.437503129786 | 8799.574667481049 | 42.8514971844432 | 93.25109944652824 | 6505.906587391383 | 2657.964927318365 |
| 11 | GHATAPRABHA | Hydro | 32.7 | 1164.231328912895 | 1134.775051866722 | 40.58042370279453 | 93.70420703328145 | 6181.793644818814 | 2800.263972089786 |
| 12 | KADRA | Hydro | 150 | 5719.481569249887 | 5578.019722142273 | 43.46555428990072 | 93.4295491862427 | 6559.261700254645 | 2724.225917343567 |
| 13 | KALMALA | Hydro | 4 | 158.2610163267722 | 154.1609989369107 | 45.05135915622773 | 93.60949887934058 | 6759.711656935922 | 2442.716747123857 |
| 14 | KG-WER | Renewable | 4.555 | 160.6034771335687 | 150.9446212930443 | 40.21472053982473 | 91.4522232962599 | 7185.063756328492 | 2807.883903532901 |
| 15 | KODSALLI | Hydro | 120 | 4461.752851644348 | 4347.692604722267 | 42.35144678516438 | 93.88859996051164 | 6708.569121720805 | 2569.489091083008 |
| 16 | LPH | Hydro | 55 | 2065.507140535222 | 2014.384288608194 | 42.79267652383455 | 93.80022422631072 | 7477.78244087144 | 2811.069708315366 |
| 17 | MALLAPUR | Hydro | 9 | 324.1808249932995 | 316.1222420993715 | 41.07061744242099 | 93.66575222549042 | 7038.034657552637 | 2582.07018423536 |
| 18 | MALPRABHA | Hydro | 2.4 | 90.76240609489581 | 88.5317568419139 | 43.15088771736736 | 93.51202753583293 | 5988.856463163554 | 2647.597263027871 |
| 19 | MANI | Hydro | 9 | 350.768116486715 | 342.0879670355029 | 44.38091452530846 | 93.4934270362598 | 7057.288378754805 | 2640.344083206395 |
| 20 | MGHE | Hydro | 139.2 | 5117.875119769744 | 4991.847046410596 | 41.9421703965993 | 94.02548836174192 | 7189.599081497346 | 2458.807886879665 |
| 21 | MUNIRABAD | Hydro | 38 | 1468.864120906346 | 1431.986415281315 | 44.0932171632377 | 93.52985035927888 | 6340.306362868662 | 2836.416586408812 |
| 22 | NPH | Hydro | 900 | 34432.86390298201 | 33576.96573886142 | 43.62574264373339 | 93.25459689041953 | 6432.170032339428 | 2615.290064001502 |
| 23 | RTPS | Thermal | 1720 | 106859.0779908221 | 97706.4765143474 | 70.86502117895506 | 90.31233046252146 | 6353.53841775669 | 2608.631235048238 |
| 24 | SGS | Hydro | 1035 | 37138.88629602548 | 36195.32884028162 | 40.92815799156386 | 93.51221049863939 | 6642.22039774428 | 2519.188160443231 |
| 25 | SH-SOLAR-10 | Solar | 10 | 165.6388997158272 | 163.1344208286853 | 18.9028721744525 | 95.42946704990139 | 6815.357969981513 | 2556.587376361422 |
| 26 | SH-SOLAR-5 | Solar | 5 | 81.90447380489874 | 80.69999061763127 | 18.69497450220587 | 95.79074057480004 | 6778.815288924384 | 2559.567061288457 |
| 27 | SHIMSHA | Hydro | 17.2 | 632.7290348178569 | 617.7824574857389 | 41.93674150764412 | 93.70369320692814 | 6107.677230096579 | 2521.134710818406 |
| 28 | SHIVA | Hydro | 42 | 1624.188781134585 | 1584.776065374539 | 44.06056020109494 | 93.59529873209529 | 6922.212348817586 | 2471.347739317805 |
| 29 | SIRWAR | Hydro | 1.02 | 38.69362956723657 | 37.73522595130876 | 43.15070815723066 | 93.85568535704938 | 6436.901532229545 | 2440.018200296676 |
| 30 | SUPA | Hydro | 100 | 3555.619337705379 | 3464.108153863515 | 40.52925410038325 | 93.6123877257123 | 6669.693665000308 | 2404.450142254883 |
| 31 | TNL-SOLAR-3 | Solar | 3.07 | 52.4885336602443 | 51.69412373663363 | 19.50076995913633 | 95.5118103091236 | 6513.992959088699 | 2635.983457931541 |
| 32 | VUGPH | Hydro | 460 | 16665.77182662529 | 16241.29333083636 | 41.24332274527478 | 93.7871781251659 | 6258.450336966255 | 2563.717579224484 |
| 33 | YAL-SOLAR-3 | Solar | 3.7 | 59.70390132838049 | 58.79470090640378 | 18.42618148759237 | 95.65353911591379 | 6794.119597876921 | 2619.449591175754 |
| 34 | YAP-SOLAR-3 | Solar | 3.07 | 53.2800937334248 | 52.49039187783044 | 19.80103589473792 | 96.01625650491104 | 6983.478360814129 | 2761.065148235969 |
| 35 | YCCPP | Combined Cycle | 370 | 18430.33340561808 | 17547.32958541879 | 56.87144468823154 | 91.12889752670614 | 7093.368987864509 | 2562.755058059698 |
| 36 | YTPS | Thermal | 1600 | 98582.43266429777 | 90129.11790687338 | 70.27324095859733 | 90.95731972222227 | 6651.832010595846 | 2673.67560642232 |

#### `Validation_Checks` — full cell dump (7r x 2c)

| Row | A | B |
|---|---|---|
| 1 | Data Validation Checks |  |
| 3 | JMR Difference Check | Difference = Final Reading - Initial Reading |
| 4 | JMR Energy Check | Difference × Meter Constant corresponds to Gross Generation |
| 5 | MPR Net Generation Check | Net Generation = Gross Generation - Auxiliary Consumption |
| 6 | MPR PLF | PLF is generated from capacity and operating hours |
| 7 | Time Range | Apr-2016 to Mar-2026 |

(A1 of `Joint_Meter_Reading` = title text; row 2 blank.)

#### `Joint_Meter_Reading` — column map (3963 rows x 13 cols); header on row 3, data starts row 4

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | Month | text/bool | 0 | 2016-04 | 2026-03 |
| B | Project | text/bool | 0 | ALAMATTI | YTPS |
| C | Plant Type | text/bool | 0 | 6 distinct | Combined Cycle, Hydro, Renewable, Solar, Thermal, Waste to Energy |
| D | Installed Capacity (MW) | number | 0 | 1.02 | 1720.0 |
| E | Meter No. | number | 0 | 1.0 | 1.0 |
| F | Initial Meter Reading (kWh) | number | 0 | 100000.0 | 105854273964.0939 |
| G | Final Meter Reading (kWh) | number | 0 | 1062527.9914 | 106859177990.8221 |
| H | Difference | number | 0 | 787795.2776 | 1113252911.635 |
| I | Meter Constant | number | 0 | 0.1 | 1.0 |
| J | Difference × Meter Constant (kWh) | number | 0 | 112146.7066 | 1113252911.635 |
| K | Gross Generation (MU) | number | 0 | 0.1121 | 1113.2529 |
| L | Auxiliary Consumption (MU) | number | 0 | 0.0021 | 108.1289 |
| M | Net Energy (MU) | number | 0 | 0.1097 | 1023.0742 |

First data row (row 4): A4=2016-04; B4=RTPS; C4=Thermal; D4=1720.0; E4=1; F4=100000.0; G4=843275547.8408082; H4=843175547.8408082; I4=1.0; J4=843175547.8408082; K4=843.1755478408081; L4=79.515661150528; M4=763.6598866902801

Last data row (row 3963): A3963=2026-03; B3963=CHANDAPUR; C3963=Solar; D3963=10.0; E3963=1; F3963=164758088.6889133; G3963=166071428.0553816; H3963=1313339.36646831; I3963=1.0; J3963=1313339.36646831; K3963=1.313339366468316; L3963=0.0204926216942789; M3963=1.292846744774037

(A1 of `Monthly_Performance_Report` = title text; row 2 blank.)

#### `Monthly_Performance_Report` — column map (3963 rows x 22 cols); header on row 3, data starts row 4

| Col | Header | Type | Nulls | Min / distinct | Max / example |
|---|---|---|---|---|---|
| A | Month | text/bool | 0 | 2016-04 | 2026-03 |
| B | Project | text/bool | 0 | ALAMATTI | YTPS |
| C | Plant Type | text/bool | 0 | 6 distinct | Combined Cycle, Hydro, Renewable, Solar, Thermal, Waste to Energy |
| D | Installed Capacity (MW) | number | 0 | 1.02 | 1720.0 |
| E | Gross Generation (MU) | number | 0 | 0.1121 | 1113.2529 |
| F | CEA/Monthly Target (MU) | number | 0 | 0.4495 | 1125.1858 |
| G | Auxiliary Consumption (MU) | number | 0 | 0.0021 | 108.1289 |
| H | Net Generation (MU) | number | 0 | 0.1097 | 1023.0742 |
| I | Maximum Generation (MU) | number | 0 | 0.16 | 1496.3077 |
| J | Peak Load (MW) | number | 0 | 0.459 | 1616.0123 |
| K | PLF (%) | number | 0 | 10.3972 | 87.8792 |
| L | Plant Availability (%) | number | 0 | 82.0731 | 99.4923 |
| M | Planned Outage Hours | number | 0 | 0.1015 | 111.5383 |
| N | Forced Outage Hours | number | 0 | 0.0079 | 44.6292 |
| O | Running Hours | number | 0 | 536.3777 | 739.8402 |
| P | Operating Factor (%) | number | 0 | 79.1778 | 99.6092 |
| Q | Heat Rate (kcal/kWh) | number | 0 | 0.0 | 2794.7783 |
| R | Coal Consumption (MT) | number | 0 | 0.0 | 2978.5045 |
| S | LDO Consumption (KL) | number | 0 | 0.0 | 29.9608 |
| T | Specific Coal Consumption (kg/kWh) | number | 0 | 0.0 | 2.7948 |
| U | Water Consumption (McuM) | number | 0 | 0.0 | 384.951 |
| V | Net PLF (%) | number | 0 | 10.2764 | 82.6612 |

First data row (row 4): A4=2016-04; B4=RTPS; C4=Thermal; D4=1720.0; E4=843.1755478408081; F4=951.6227493132341; G4=79.515661150528; H4=763.6598866902801; I4=1171.0771497789; J4=1264.763321761212; K4=68.08588080109885; L4=95.02476074987449; M4=38.16249266362092; N4=13.85976317259948; O4=667.9777441637796; P4=92.77468668941383; Q4=2576.26633718618; R4=2172.24478024079; S4=8.903865998099935; T4=2.576266337186181; U4=0.0; V4=61.66504252989987

Last data row (row 3963): A3963=2026-03; B3963=CHANDAPUR; C3963=Solar; D3963=10.0; E3963=1.313339366468316; F3963=6.046429682974024; G3963=0.0204926216942789; H3963=1.292846744774037; I3963=1.76524108396279; J3963=4.5; K3963=17.6524108396279; L3963=97.3962900406573; M3963=52.98207331908307; N3963=7.214870764446677; O3963=683.8030559164703; P3963=91.90901289199869; Q3963=0.0; R3963=0.0; S3963=0.0; T3963=0.0; U3963=0.0; V3963=17.37697237599512


---

# Part C — Voice instance (added 2026-10-02)

A **separate copy** of the app with a hands-free voice agent, modelled on the Meghalaya voice agent. It lives in
`webapp/kpcl-voice-app/` and runs on **port 8001**. The original app (`webapp/kpcl-ai-app/`, port 8000) was not edited
in any way: a SHA-256 fingerprint of every pre-existing file was taken before the work and compared after it, and
only `webapp/.env` changed (see C.4). Full detail is in `webapp/kpcl-voice-app/README.md`.

## C.1 Run

```powershell
# voice instance (the original keeps running on 8000)
Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Set-Location "D:\KPCL POC\KPCL POC\webapp\kpcl-voice-app"; python -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# tests: 600 (548 inherited + 52 voice)
cd "D:\KPCL POC\KPCL POC\webapp\kpcl-voice-app"; python -m pytest tests -q -p no:cacheprovider
```

Open http://127.0.0.1:8001/chat and press the round button beside **Ask**. The browser shows Allow / Block for the
microphone; after Allow, voice starts listening. The dashboard and insights are at `/` and `/insights` on the same port.

Conditions:
- **VPN:** speech in and out run on the same internal gateway as the SQL model, so the VPN must be connected.
- **Secure address:** the microphone works only on `127.0.0.1` / `localhost` or HTTPS.

## C.2 Decisions (the user's, 2026-10-02)
- **Separate copy:** the app was duplicated rather than modified; no edits to the old code.
- **Settings:** Meghalaya's `.env` was merged into KPCL's.
- **Spoken answers:** built from the data with no model call (the fastest option, and figures match the screen).
- **Security:** the browser setting the speech detector needs (`'wasm-unsafe-eval'`) and the microphone permission
  are approved for the voice instance only.
- **Voice:** Meghalaya's officer voice clip, with a KPCL persona; English only.
- **Pages:** the full set (dashboard, insights, chat) on the voice instance.
- **`D:\Meghalaya_voice` is strictly read-only.** Files were only copied out of it: the officer clip, the
  speech-detector files and the `.env` values. Same rule as the WB and Cescom reference folders.

## C.3 How it works
- **Turn loop:** speech detection in the browser (Silero VAD, with a loudness fallback) → `POST /api/voice/transcribe`
  (qwen3-asr) → a filler phrase → the unchanged `/api/ask` with `mode:"voice"` → per sentence
  `POST /api/voice/speak/stream` (Qwen3-TTS, Base task, cloning `voices/officer_serena.wav`, with the KPCL persona as
  the style instruction) → back to listening.
- **Barge-in:** speaking over a reply interrupts it, and those words become the next question.
- **Answers in the chat:** spoken answers also appear in the chat exactly like typed ones, with chart, table and
  View / Download report.
- **Early start:** the SSE stream sends a `spoken` event before the answer text, so speech starts while the text is
  still typing out.
- **What is said** (`app/spoken.py`):
  - the answer's headline, plus at most one supporting sentence with a new figure;
  - figures rounded the way officers say them;
  - "the full details, and the report, are on your screen" when there's more to see.
  - A test checks that every spoken figure is a rounding of a figure on screen.
- **Pronunciation:** `pronounce()` spells out codes (RTPS → "R T P S") and reads FYs and units naturally.
- **Transcript clean-up:** `clean_transcript()` maps "R.T.P.S." to RTPS and "two thousand and twenty three twenty four"
  to 2023-24, so voice questions reach the instant deterministic routes.
- **Security, voice copy only:**
  - `microphone=(self)`;
  - CSP adds `'wasm-unsafe-eval'` (WebAssembly only; JavaScript eval stays blocked);
  - a 10 MB body cap on `/api/voice/transcribe` only; every other body keeps the 16 KB cap;
  - a separate per-IP budget for `/api/voice/*`.

## C.4 Settings merge (`webapp/.env`)
All 86 Meghalaya keys that the original app does not read were appended under a marked block. Four keys were **not**
copied, and KPCL's values were kept: `SQL_VERIFY_MODEL`, `SQL_VERIFY_BASE_URL`, `SQL_VERIFY_API_KEY` and
`AI_MODEL_CA_BUNDLE_PATH`. Meghalaya's CA path is relative to its own folder and would break the original on its next
restart. The original app's loaded settings were dumped before and after the merge and are identical.

Two things to know:
- **Unused persona line:** Meghalaya's own `TTS_INSTRUCTIONS` (a Khasi persona) is in the file but is not used. The
  voice copy reads `KPCL_VOICE_INSTRUCTIONS`, whose default is the KPCL persona.
- **More secrets in one file:** the merged file now also holds Meghalaya's other secrets (database URL, JWT secret,
  model keys). Never share or zip `webapp/.env`.

## C.5 Verification (2026-10-02)
- **pytest (voice copy):** 600 passed, with the VPN up.
- **Live round trip through the gateway:** three spoken questions, generated with Windows speech.
  - Speech-to-text: 0.1–1.2 s, every word right.
  - Answers: correct.
  - Officer voice: clean 24 kHz audio, first audio about 1.1–1.7 s after a sentence is sent.
- **Headless Edge with a fake microphone** (a recorded question), at desktop and phone widths:
  - the button opened the microphone and Silero VAD loaded;
  - the question was heard, answered in the chat with View / Download report, and spoken;
  - it returned to Listening;
  - no console errors and no security-policy violations.
- **Live stress bank against port 8001:** **213/213**. All 213 questions are answered the same as on port 8000. A first attempt ran two stress runners at once by mistake and 65 answers were rate-limit refusals; re-asked alone, all passed. Run one runner at a time.
- **Still needed:** a real-room test with a person and a real microphone (noise, accents, barge-in).

## C.6 Gotchas
- **Restart after changes:** restart 8001 after changing its code (no auto-reload). The two servers are independent;
  restarting one never touches the other.
- **Stress runner:** the copy's `tests/run_stress.py` defaults to port 8001 and writes
  `webapp/kpcl-voice-app/STRESS_TEST_QUESTIONS_VOICE.md`, never the original's `STRESS_TEST_QUESTIONS.md`.
- **Gateway models:** the gateway serves only the cloning (Base) TTS model; preset voices (`CustomVoice`) return 400.
  If the model is down, the page uses the browser's own voice with the same text.
- **Kannada:** the switch translates the voice panel's labels only; speech in and out are English.
- **Duplicate drift:** a fix made in `kpcl-ai-app` is not automatically in `kpcl-voice-app` (it is a copy), so apply
  fixes to both.

## C.7 Voice stress test (2026-10-02)
- **The reported failure:** "How much did Sharavati generate in FY 2018 to 2019?" worked typed but not spoken. The
  speech model wrote "Sharavati" (no h), which the station list doesn't know, so the question fell through to the
  AI model, whose answer-checking pass rejected the answer. Years like "F.Y. 2018 to 2019" or "FY18-19" also missed
  the instant routes.
- **The runner:** `tests/run_voice_stress.py` in `webapp/kpcl-voice-app` (VPN needed). It covers every stress-bank
  question (207 speakable ones) plus 94 generated ones: every station by its spoken name, units, dates, reservoirs and
  coal suppliers.
  - Each question is spoken by Windows Indian-English voices: Ravi says codes letter by letter and "F Y twenty
    eighteen nineteen"; Heera uses station names and "financial year twenty eighteen to twenty nineteen".
  - Each recording goes through the real speech-to-text and the voice pipeline, and is checked against the same
    independent expected figures as the typed test.
  - A single figure must also be in what the voice says.
  - Failures are re-asked typed, to separate voice problems from pipeline problems.
  - Writes `VOICE_STRESS_TEST_QUESTIONS.md` and `tests/voice_stress_report.json`.
- **Result: 496/602 on the first run, 594/602 (98.7%) after the fixes.**
- **Fixes, all in `kpcl-voice-app`:**
  - a one-paragraph context prompt for speech-to-text (stations, reservoirs, suppliers, terms) replaces the comma hint
    list. The list made the model mishear years and sometimes return the list itself, which emptied the transcript;
  - an echo filter for that prompt;
  - in `app/spoken.py` `clean_transcript`:
    - merged-year repair ("FY 2020 to 23" → FY 2022-23, "2020 even 22" → 2021-22);
    - spoken dates ("twenty sixth January 2020" → 26 January 2020);
    - "FY18-19" and "financial year 24";
    - phrase fixes (power house, mini hydel, shear pin, running hours, megawatt → MW, quarter two → Q2, unit for → unit 4);
    - a sound-based name match against the data's own station, reservoir and supplier names;
    - context matching (a word before "mini hydel" can only be a mini hydel, and so on);
    - a short list of observed mishearings (Jerusalem → Gerusoppa, Money dam → Mani dam, Natarrey → Nagjhari).
  - Silence, noise, mains hum and a chime all give an empty transcript.
- **The 8 that still fail:**
  - Sirwar heard as "Shiva", a real, different station, so the answer is confidently about the wrong station (2).
  - Two questions where "twenty twenty-one twenty-two" was heard as a clean "FY 2020-21" (wrong year, said back in the
    answer).
  - One where "Sharavati" was heard as an unrelated word: a safe refusal.
  - Three where the AI model answered inconsistently: the transcript was exact, and the typed question passes on
    some runs.
  - The first two kinds need guardrails such as read-back or confirmation (see the guardrail list given to the user).

## C.8 Voice guardrails (2026-10-02)

All in `webapp/kpcl-voice-app` (the 8000 app is untouched). Full table: `webapp/kpcl-voice-app/README.md` → "Voice guardrails".

- **Where they sit:**
  - `app/voice_guard.py`: server-side checks;
  - `app/main.py` `_spoken_for_voice` and `POST /api/voice/transcribe`: wiring;
  - `app/static/voice/voice-agent.js` and `voice-ui.js`: browser side;
  - `app/voice_lexicon.json`: the reviewed misheard-names list.
- **Transcribe** now returns `{text, heard, confirm, rejected, message}`. `rejected` is one of `too_long`, `language`, `duplicate` or `echo`. It answers 503 when speech-to-text is down, and the panel switches to "type your question instead".
- **Order for every spoken answer:**
  1. ask again on a refusal that may come from mishearing;
  2. a model-written answer is read out only if its second check passed;
  3. otherwise the answer is read back as "For <place>, <period>: …".
- **Spoken-figure checks.** These apply in step 3; if they fail, the voice says "The figures are on your screen" and no number.
  - Every spoken figure must be a screen figure. Lakh and crore are compared both ways.
  - Each figure needs one of:
    - its unit after it;
    - its unit in the label ("lakh tons: 65.33");
    - for a whole number, what it counts named earlier in the sentence ("unit trips … was 33").
  - Heat rate and frequency get "kilocalories per kilowatt-hour" and "hertz" said, because the data always keeps them in those units.
  - A bare decimal is never said.
- **Confirmation ("Did you mean A, or B?")** appears for:
  - a loose name match;
  - a repaired or ambiguous year ("FY24" → FY 2023-24 or FY 2024-25);
  - the Shiva / Sirwar pair.

  The officer answers by voice or taps an option.
- **Browser:**
  - "I heard: …" window for 2 s with "That's not what I said";
  - "Still working on it." after 8 s; stop waiting after 60 s;
  - microphone indicator; off after 3 minutes idle or when the tab is hidden;
  - 30 s recording cap;
  - health dots: speech in and voice, polled every 3 minutes.
- **Growing the misheard-names list:**
  - Collection is off by default (`VOICE_COLLECT_MISHEARINGS=true`). It records text only, and officers must consent.
  - Review with `python tests/review_mishearings.py`, then add with `--add heard=Name --reviewer "<who>"`.
  - An entry that is (or sounds like) a real name or an ordinary word is rejected.
  - Do not add entries from the synthetic test voices.
- **Results:**
  - `pytest`: 686 passed.
  - Headless Edge with a fake microphone: normal turn, Shiva/Sirwar tap, "That's not what I said", speech-to-text down → type instead, hidden tab → microphone off. No console errors or CSP violations.
  - Voice stress test: **597/602** (was 594). 31 questions (5%) asked "Did you mean …?", and each was resolved to the intended option. No answer had its figures withheld.
- **The first guardrail run (575/602) found five wording bugs in the spoken form, now fixed:**
  - "31 March 2,022";
  - "megawattsh";
  - "cross the 33 stations" (dropped "A");
  - ISO dates read raw;
  - "MMSCM million standard cubic metres".
- **The 5 that still fail:**
  - 2: "twenty twenty-one twenty-two" heard as a clean "FY 2020-21". The read-back says the wrong year aloud, so the officer hears it.
  - 2: "Sharavathi" heard as "Shivakoti" / "Shivatati". The voice asks again; no wrong answer.
  - 1: the AI model answered inconsistently on an exact transcript. The voice asks again.

## C.9 Listening fixes after hands-on testing (2026-10-02)

What the user reported, and the causes:

- **Answers cut off, then back to "Listening".** While a reply played, any voice-like sound stopped it at once: the agent's own voice from the laptop speakers, or the room. That sound was then taken as the next question. The speech detector was also restarted at every sentence.
- **"PLF" heard when nobody spoke.** Voice mode recorded with a plain loudness detector before the speech detector had loaded. The start chime, a key press or a fan became a "question". The speech model, primed with KPCL terms, then wrote "PLF" for that noise.

Fixes, all in `webapp/kpcl-voice-app`:

- **Talking over a reply** (`voice-agent.js` `onVadSpeechStart` / `checkBargeIn`):
  - the reply is turned down to a quarter, not stopped;
  - when the speaker stops, the words are transcribed (`expect=bargein`);
  - the reply's own echo, a fragment or a cough → the reply carries on at full volume;
  - a real question or "stop" → the reply ends and the question is handled;
  - the check runs from the moment the answer is looked up until the reply ends. Hold-to-talk and the no-detector fallback interrupt by press or tap only.
- **Echo** (`voice-text.js` `isEchoOf`): a transcript that repeats a run of four or more words of the last reply, begun while it played (or within 3 s of the end), is ignored. A follow-up question shares words with the reply, but not a run of four or more, so it is not ignored.
- **Start-up and between turns:**
  - listening starts only once the speech detector is ready;
  - 0.3 s of quiet after each reply;
  - detector thresholds 0.6 / 600 ms (were 0.5 / 550 ms);
  - the loudness fallback adapts to the room and needs 0.5 s of sound;
  - the chat is given one question at a time: the agent waits for it to finish the previous answer.
- **Server** (`voice_guard.voiced_seconds`, `too_short`):
  - a recording with under 0.35 s of sound clearly above its own background never reaches the speech model (`rejected: no_speech`);
  - a one-word transcript, or 2–3 hint words from under a second of sound, is dropped silently (`too_short`; the panel shows "I only heard …").
  - `expect=confirm` allows one-word answers to "Did you mean …?".

Checked:

- `pytest`: 693 passed.
- None of the 602 stress-test recordings would be dropped by the new filters (shortest real question: 0.78 s of speech).
- Headless Edge with a fake microphone:
  - **echo:** the reply played back into the microphone; the reply plays to the end, and nothing is taken as a question;
  - **noise:** fan, typing, coughs and thumps; nothing is sent;
  - **barge-in:** a real question said over the reply; the reply stops and that question is answered;
  - the five earlier guardrail scenarios still pass;
  - no console errors and no CSP violations.
- To debug on a real machine: `window.__voiceDebug = true` in the browser console.
