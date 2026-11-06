# KPCL GMS warehouse (SQLite)

`build_warehouse.py` turns `../KPCL_GMS_10yr.xlsx` and `../document_anchors.json` into one read-only-to-the-app SQLite file, `kpcl_gms.db` (~460 MB). The PostgreSQL files in the parent folder (`gms_schema.sql`, `gms_mart.sql`, `load_gms.py`, …) are unchanged; this is their SQLite implementation, layered like the WB POC warehouse.

## Layers (table prefix = layer)

| Prefix | What | Chatbot may read? |
|---|---|---|
| `src_` | registry of source files (sha256) and sheets; every cell (value + formula) of the 8 small/irregular sheets | no |
| `ref_` | `document_anchors.json` flattened to 1 row per value (`ref_document_anchor`) | no |
| `raw_` | 19 tabular sheets, value-for-value as TEXT, with the Excel row number | no |
| `stg_` | typed copies + `quality_flags` JSON per row; `stg__column_type` records the inferred type of every column | no |
| `core_` | the normalised model: `schema_core.sql`, a reviewed SQLite translation of `gms_schema.sql` (47 tables, 23 loaded) | no |
| `mart_` | 22 query-ready tables (`mart.sql`): 8 dimensions, 14 facts, each fact carrying fy / month_label / water_year / season and station / category so most questions need no join | **yes, only these** |
| `semantic_` | table + column docs, metric registry, capability manifest, data period | no (exported to JSON) |
| `ledger_` | column lineage (every raw column → where it landed, or why not), known issues, reconcile log, the 71 reconciliation checks, build checks, build timings | no |

## Stages

`sources → raw → staging → core → reconcile → mart → semantic → ledger → checks` (about 1½ minutes in all).

```
python build_warehouse.py                  # full build
python build_warehouse.py --from mart      # rebuild mart and later stages
python build_warehouse.py --to reconcile   # stop early
python ../webapp/kpcl_semantic_layer/build_semantic_layer.py   # then regenerate the chatbot's semantic layer + integrity manifest
```

* **reconcile** (`reconcile.py`) ports `../reconcile_to_document.py` steps 1-3 and all 71 checks of `../verify_reconciliation.py` (same rules, same tolerances). Result: **71 PASS / 0 FAIL**; every rescale factor is ×1.0000, only the 06-Apr-2025 RTPS/SGS unit figures change. Two safety differences, both recorded as known issues: on 06-Apr-2025 RTPS planned/RSD hours are set to 0 so hours still add to 24 (KI-11), and declared capacity is never below gross (KI-07; the workbook's 2-decimal PAF put 2 unit-days just below PLF).
* **mart** marks every row with `anchor_scope`: which totals are pinned to the K-eRP document (`document_day`, `document_month_total`, `document_fy_total`), estimated from the HO report (`workbook_fy_estimate`), or purely synthetic (NULL). A roll-up keeps a scope only if every row under it has it.
* **checks** (10, all PASS on the current build): mart FY and month totals equal the workbook's own `annual_station_summary` (308 station-years) and `monthly_station_summary` (3,684 station-months); daily row count; unit-day total = station-FY total; hours add to 24; KPCL FY 2024-25 = 37,602.2 MU; RTPS PLF March 2025 = 64.95 %; units out at data end = RTPS-U1, RTPS-U4, SIRWAR-U1; 71/71 anchors; every mart column documented.

## Metadata (`metadata/`, hand-written, the single source of truth)

| File | Used for |
|---|---|
| `semantic_docs.json` | grain + doc of every mart table and column (the build fails on an undocumented column), unit suffixes, station / reservoir / category aliases, glossary (technical + plain), prompt rules |
| `metric_registry.json` | 28 KPIs: synonyms, unit, precision, SUM/SUM expressions per table family, source table and filter, which plant types they apply to; plus the non-averageable / non-summable column lists used by the SQL guard |
| `capability_manifest.json` | 18 topics, loaded / not_loaded / partial, with keywords and the note the chatbot gives; the build fails if a status disagrees with the row counts |
| `known_issues.json` | 11 known issues (KI-01 … KI-11) with the one-line caveat attached to affected answers |
| `_lineage.json` | written by the core stage (raw column → core column) |
