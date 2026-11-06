"""
Capability check (no LLM, no SQL): a question that needs data this warehouse does not hold -- JMR, billing,
commercial receipts, defects, hourly readings, common auxiliary, HR, KERC targets, holidays -- gets the
reviewed note from the capability manifest instead of a guessed or empty answer.
"""
from __future__ import annotations

import re

from app.semantic_layer import load_semantic_layer, norm


def _loaded_summary() -> str:
    topics = [c for c in load_semantic_layer()["capabilities"] if c["status"] == "loaded"]
    names = {"generation": "generation and performance (PLF, PAF, aux, fuel, hours)", "backdown": "back-down losses",
             "outages": "outages, trips and boiler tube failures", "targets": "CEA and internal targets",
             "coal": "coal stock and receipts", "reservoirs": "reservoir levels", "state_grid": "the state grid"}
    return ", ".join(names.get(t["topic"], t["topic"]) for t in topics)


# A clock time ("2 pm", "14:00", "between 2 and 3 pm") asks for sub-daily readings; the data is daily.
_TIME_OF_DAY_RE = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])|\b([01]?\d|2[0-3]):[0-5]\d\b|"
                             r"\b(noon|midnight|morning|evening|night) (of|on)\b")


def check(question: str) -> str | None:
    q = norm(question)
    caps = [c for c in load_semantic_layer()["capabilities"] if c["status"] == "not_loaded"]
    for cap in caps:
        for kw in cap.get("keywords", []):
            if re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", q):
                return _note(cap)
    if _TIME_OF_DAY_RE.search(q):
        hourly = next((c for c in caps if "hourly" in c.get("keywords", [])), None)
        if hourly:
            return _note(hourly)
    return None


def _note(cap: dict) -> str:
    return (f"**{cap['note']}**\n\nWhat this data does cover, for all 33 stations from April 2015 to "
            f"April 2025: {_loaded_summary()}.")
