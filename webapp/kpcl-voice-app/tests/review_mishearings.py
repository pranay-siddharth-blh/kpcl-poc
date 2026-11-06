"""
Grow the misheard-names list (app/voice_lexicon.json) carefully, from real officer recordings collected with consent.

  Collection (off by default): set VOICE_COLLECT_MISHEARINGS=true in webapp/.env and restart port 8001. Each spoken
  question whose transcript was corrected, was uncertain, or named something the data doesn't know is appended to
  logs/voice_mishearings.jsonl as TEXT only (what was heard, the cleaned question, the notes). No audio is kept.

  python tests/review_mishearings.py                     list the unrecognised words, most frequent first, with the
                                                        closest real station / reservoir name as a suggestion
  python tests/review_mishearings.py --add worahi=Varahi --reviewer "A. Officer"
                                                        add one entry, only if it passes the same validation the
                                                        server applies (never a real name, never an ordinary word,
                                                        and it must mean a name the data knows); then run the tests
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import voice_guard  # noqa: E402
from app.spoken import _name_vocab, _phon  # noqa: E402


def suggestions(word: str, n: int = 2) -> list[str]:
    vocab = _name_vocab()
    k = _phon(word)
    ranked = sorted(vocab, key=lambda x: difflib.SequenceMatcher(None, k, x).ratio(), reverse=True)[:n]
    return [f"{vocab[x]} ({difflib.SequenceMatcher(None, k, x).ratio():.2f})" for x in ranked]


def list_candidates() -> None:
    log = voice_guard.MISHEARINGS_LOG
    if not log.exists():
        print(f"No collected transcripts yet ({log}). Collection is "
              f"{'ON' if voice_guard.settings.voice_collect_mishearings else 'OFF (VOICE_COLLECT_MISHEARINGS=false)'}.")
        return
    words, examples = collections.Counter(), {}
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        for w in e.get("unknown_names") or []:
            words[w.lower()] += 1
            examples.setdefault(w.lower(), e.get("heard", ""))
        for n in e.get("notes") or []:
            words[f"(unsure) {n.get('span', '')}"] += 1
            examples.setdefault(f"(unsure) {n.get('span', '')}", e.get("heard", ""))
    known = voice_guard.lexicon()["mishearings"]
    print(f"{'count':>5}  {'word':24s}  closest real names               example heard")
    for w, c in words.most_common(50):
        plain = w.replace("(unsure) ", "")
        mark = " [already in list]" if plain in known else ""
        print(f"{c:>5}  {w:24s}  {', '.join(suggestions(plain)):32s}  {examples[w][:70]}{mark}")


def add(pair: str, reviewer: str) -> int:
    heard, _, means = pair.partition("=")
    heard, means = heard.strip().lower(), means.strip()
    if not heard or not means or not reviewer:
        print("usage: --add <heard>=<Name> --reviewer <who reviewed it>")
        return 2
    path = voice_guard.LEXICON_PATH
    doc = json.loads(path.read_text(encoding="utf-8"))
    if any(e["heard"] == heard for e in doc["mishearings"]):
        print(f"{heard!r} is already in the list.")
        return 1
    entry = {"heard": heard, "means": means, "source": "officer recordings (consented), reviewed with review_mishearings.py",
             "added": dt.date.today().isoformat(), "reviewed_by": reviewer}
    doc["mishearings"].append(entry)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    voice_guard.lexicon.cache_clear()
    rejected = [why for e, why in voice_guard.lexicon()["rejected"] if e["heard"] == heard]
    if rejected:                                           # same validation as the server: undo and explain
        doc["mishearings"] = [e for e in doc["mishearings"] if e["heard"] != heard]
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"NOT added: {heard!r} -> {means!r}: {rejected[0]}")
        return 1
    print(f"added: {heard!r} -> {means!r} (reviewed by {reviewer}). Restart port 8001 and run the tests.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", default="")
    ap.add_argument("--reviewer", default="")
    a = ap.parse_args()
    sys.exit(add(a.add, a.reviewer) if a.add else (list_candidates() or 0))
