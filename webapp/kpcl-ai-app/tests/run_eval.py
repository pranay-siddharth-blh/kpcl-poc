"""
Live evaluation through the real pipeline and the real gateway (needs the VPN), in the style of the WB POC's
run_eval_v2: every question in eval_questions.py is asked, and the figures of its reference SQL's first row must
appear in the answer text or in the returned rows. Writes tests/eval_report.json and prints a summary.

run from webapp/kpcl-ai-app:   python tests/run_eval.py [--ids 1,5]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from app.config import settings  # noqa: E402
from app.db import ReadOnlyDB  # noqa: E402
from app.llm import init_llm_client, shutdown_llm_client  # noqa: E402
from app.pipeline import answer_question  # noqa: E402
from eval_questions import QUESTIONS  # noqa: E402


def _forms(v: float) -> list[str]:
    out = {f"{v:,.0f}", f"{v:.0f}"} if abs(v - round(v)) < 1e-9 else {f"{v:,.{p}f}" for p in (1, 2, 3)} | {f"{v:.{p}f}" for p in (1, 2, 3)}
    return sorted(out, key=len, reverse=True)


def _station_names() -> dict[str, str]:
    from app.semantic_layer import load_semantic_layer
    return {s["key"]: s["name"] for s in load_semantic_layer()["entities"]["stations"]}


def _found(v, text: str, rows: list) -> bool:
    if isinstance(v, str):
        names = [v, _station_names().get(v, v)]  # a station may be named by code or full name
        return any(n.lower() in text.lower() or any(n == c for r in rows for c in r) for n in names)
    t = text.replace("₹", "")
    if any(re.search(r"(?<![\d.,])" + re.escape(f) + r"(?![\d])", t) for f in _forms(float(v))):
        return True
    return any(isinstance(c, (int, float)) and abs(c - v) <= max(0.01, abs(v) * 0.0005) for r in rows for c in r)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    args = ap.parse_args()
    ids = {int(x) for x in args.ids.split(",")} if args.ids else None
    if init_llm_client() is None:
        print("LLM gateway not configured in webapp/.env -- the LLM-path questions will fail.")
    db = ReadOnlyDB(settings.db_path)
    report, passed = [], 0
    for item in QUESTIONS:
        if ids and item["id"] not in ids:
            continue
        ref = db.run_sync(item["ref"], 5).rows[0]
        t0 = time.monotonic()
        r = await answer_question(item["q"], db, settings.kpcl_max_rows, settings.kpcl_query_timeout_seconds)
        ms = round((time.monotonic() - t0) * 1000)
        ok = r.answerable and all(_found(v, r.answer, r.rows) for v in ref if v is not None)
        passed += ok
        report.append({"id": item["id"], "question": item["q"], "pass": ok, "expected": list(ref), "route": r.route,
                       "answer": r.answer, "sql": r.sql, "error": r.error, "ms": ms, "timing_ms": r.timing_ms})
        print(f"{'PASS' if ok else 'FAIL'}  #{item['id']:>2}  {ms:>6} ms  {r.route:<28} {item['q']}")
        if not ok:
            print(f"        expected {list(ref)}\n        got: {r.answer[:300]!r}\n        sql: {r.sql[:300]!r} {r.error}")
    db.close()
    await shutdown_llm_client()
    (HERE / "eval_report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"\n{passed}/{len(report)} passed; report written to tests/eval_report.json")


if __name__ == "__main__":
    asyncio.run(main())
