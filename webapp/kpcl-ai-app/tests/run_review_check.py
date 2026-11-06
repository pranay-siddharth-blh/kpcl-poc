"""
Live check of the answer reviewer (second model pass) against the real gateway: known-wrong answers must be rejected
(and survive the deterministic veto), known-right answers must pass. The SQL runs on the real warehouse; the answer
text is written by the real answer writer, exactly as in the pipeline.

Usage (VPN up):  python tests/run_review_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import answer as writer  # noqa: E402
from app import llm, review  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import ReadOnlyDB  # noqa: E402
from app.semantic_layer import extract_entities, load_semantic_layer  # noqa: E402

M = "mart_fact_unit_generation_monthly"
S = "mart_fact_station_generation_fy"
CASES = [
    # (question, SQL, template, must_reject)
    ("Give me the PAF of each BTPS unit for March 2025",
     f"SELECT unit_key AS unit, ROUND(SUM(gross_gen_mu) * 100.0 / SUM(capacity_mu), 2) AS plf_pct FROM {M} "
     "WHERE station_key = 'BTPS' AND month_label = 'MAR-2025' GROUP BY unit_key", "", True),                 # PLF, not PAF
    ("What was RTPS's PLF in FY 2023-24?",
     f"SELECT ROUND(gross_gen_mu * 100.0 / capacity_mu, 2) AS plf_pct FROM {S} WHERE station_key = 'BTPS' AND fy = '2023-24'",
     "RTPS PLF in FY 2023-24 was {plf_pct}%.", True),                                                       # wrong station
    ("What was YTPS generation in FY 2022-23?",
     f"SELECT ROUND(gross_gen_mu, 3) AS gross_gen_mu FROM {S} WHERE station_key = 'YTPS' AND fy = '2021-22'",
     "YTPS generated {gross_gen_mu} MU in FY 2022-23.", True),                                               # wrong year
    ("What was Karnataka's electricity consumption in March 2025?",
     "SELECT date_key, ROUND(state_consumption_mu, 3) AS state_consumption_mu FROM mart_fact_grid_daily "
     "WHERE year_month = '2025-03'", "", True),                                                              # daily rows
    ("How much did SGS generate in FY 2024-25?",
     f"SELECT ROUND(gross_gen_mu, 3) AS gross_gen_mu FROM {S} WHERE station_key = 'SGS' AND fy = '2024-25'",
     "SGS generated 5,912.004 MU in FY 2024-25, its best year ever.", True),                                 # invented figure
    ("What was the auxiliary consumption percentage of BTPS in FY 2022-23?",
     f"SELECT ROUND(aux_consumption_mu, 3) AS aux_consumption_mu FROM {S} WHERE station_key = 'BTPS' AND fy = '2022-23'",
     "BTPS auxiliary consumption was {aux_consumption_mu} MU in FY 2022-23.", True),                        # MU, not %
    # controls: right answers
    ("Give me the PAF of each BTPS unit for March 2025",
     f"SELECT unit_key AS unit, ROUND(SUM(declared_capacity_mu) * 100.0 / SUM(capacity_mu), 2) AS paf_pct FROM {M} "
     "WHERE station_key = 'BTPS' AND month_label = 'MAR-2025' GROUP BY unit_key", "", False),
    ("What was YTPS generation in FY 2022-23?",
     f"SELECT ROUND(gross_gen_mu, 3) AS gross_gen_mu FROM {S} WHERE station_key = 'YTPS' AND fy = '2022-23'",
     "YTPS generated {gross_gen_mu} MU in FY 2022-23.", False),
    ("Which station had the highest PLF in FY 2024-25?",
     f"SELECT station_key AS station, ROUND(gross_gen_mu * 100.0 / capacity_mu, 2) AS plf_pct FROM {S} "
     "WHERE fy = '2024-25' ORDER BY plf_pct DESC LIMIT 5", "", False),
    ("What was Karnataka's electricity consumption in March 2025?",
     "SELECT ROUND(SUM(state_consumption_mu), 3) AS state_consumption_mu FROM mart_fact_grid_daily WHERE year_month = '2025-03'",
     "Karnataka consumed {state_consumption_mu} MU in March 2025.", False),
]


async def main():
    load_semantic_layer()
    llm.init_llm_client()
    db = ReadOnlyDB(settings.db_path)
    right = 0
    for q, sql, template, must_reject in CASES:
        qr = await db.run(sql, max_rows=200, timeout_seconds=20)
        e = extract_entities(q)
        text = writer.present(writer.fill_template(template, qr.columns, qr.rows, e))
        verdict, note = await review.review_answer(q, e, sql, qr.columns, qr.rows, text)
        ok = (not verdict.ok) == must_reject
        right += ok
        print(f"{'PASS' if ok else 'FAIL'}  expected {'REJECT' if must_reject else 'ACCEPT'}, got "
              f"{'ACCEPT' if verdict.ok else 'REJECT'}  | {q}\n      {note or verdict.problem or '-'}"[:400])
    print(f"\n{right}/{len(CASES)} correct")
    await llm.shutdown_llm_client()
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
