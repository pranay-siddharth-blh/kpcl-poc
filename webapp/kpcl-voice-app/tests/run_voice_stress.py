"""
Voice stress test: every stress-bank question plus a generated bank covering every station by its spoken name, units,
dates, months, reservoirs and coal suppliers is SPOKEN (Windows voices, Indian English: Ravi and Heera), sent through
the real speech-to-text (/api/voice/transcribe, qwen3-asr on the gateway, VPN needed) and then through this app's
voice pipeline (/api/ask_once with mode "voice"), in-process, so the per-IP rate limits don't throttle the run.

Each spoken question is checked exactly like the typed stress test (expected figures recomputed from the warehouse by
independent SQL) and, for single-figure answers, the spoken answer must carry that figure (rounded as said). A failure
is re-asked as typed text: if the typed question passes, the problem is the voice layer (hearing / clean-up); if it
fails too, it is the pipeline and not voice-specific.

Two speaking styles per question:
  A  Ravi (Indian English, male):   codes letter by letter ("R T P S"), "F Y twenty eighteen nineteen"
  B  Heera (Indian English, female): station names ("Raichur thermal"), "financial year twenty eighteen to twenty nineteen"

usage (from webapp/kpcl-voice-app):  python tests/run_voice_stress.py [--only 4,136] [--failed] [--limit N] [--workers 4]
Writes tests/voice_stress_report.json and VOICE_STRESS_TEST_QUESTIONS.md (this folder only).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

os.environ["KPCL_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["VOICE_RATE_LIMIT_PER_MINUTE"] = "100000"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import httpx  # noqa: E402

from run_stress import check  # noqa: E402  (same checks as the typed stress test)
from stress_bank import BANK, GEN, PLF, N, ud  # noqa: E402

DB = HERE.parents[2] / "warehouse" / "kpcl_gms.db"
WAVS = Path(os.environ.get("VOICE_WAV_DIR", str(Path(os.environ.get("TEMP", HERE)) / "kpcl_voice_wavs")))
REPORT = HERE / "voice_stress_report.json"
MD = HERE.parent / "VOICE_STRESS_TEST_QUESTIONS.md"
# Typed-only cases: symbols and code that nobody speaks (they stay covered by the typed stress test).
UNSPEAKABLE = {167, 186, 198, 199, 201, 202}

# ------------------------------------------------------------------ how a person says a written question
ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
ORD = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}
MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June", "jul": "July",
          "aug": "August", "sep": "September", "oct": "October", "nov": "November", "dec": "December"}
NAMES = {"RTPS": "Raichur thermal", "BTPS": "Bellary thermal", "YTPS": "Yeramarus thermal", "SGS": "Sharavathi",
         "NPH": "Nagjhari", "VUGPH": "Varahi underground power house", "MGHE": "Mahatma Gandhi hydro electric station",
         "LPH": "Linganamakki power house", "YCCPP": "Yelahanka combined cycle plant", "BWTEP": "Bidadi waste to energy plant"}
LETTERS = ["KPCL", "PLF", "PAF", "CEA", "CUF", "GCV", "JMR", "TMC", "API", "LLM", "IPL", "HFO", "LDO", "MCL", "SCCL", "WCL",
           "RTPS", "BTPS", "YTPS", "SGS", "NPH", "VUGPH", "MGHE", "LPH", "YCCPP", "BWTEP"]


def two(n: int) -> str:
    return ONES[n] if n < 20 else TENS[n // 10] + (f"-{ONES[n % 10]}" if n % 10 else "")


def year(y: int) -> str:
    return ("two thousand" + (f" {ONES[y - 2000]}" if y > 2000 else "")) if 2000 <= y <= 2009 else f"{two(y // 100)} {two(y % 100)}"


def ordinal(d: int) -> str:
    if d in ORD:
        return ORD[d]
    w = two(d)
    if d > 20 and d % 10 in ORD:
        return w.rsplit("-", 1)[0] + "-" + ORD[d % 10]
    return (w[:-1] + "ieth") if w.endswith("y") else w + "th"


def speakify(q: str, style: str) -> str:
    s = q
    s = re.sub(r"\b(\d{1,2})[- ]([A-Za-z]{3})[a-z]*[- ](\d{4})\b",
               lambda m: (f"{ordinal(int(m.group(1)))} {MONTHS[m.group(2).lower()[:3]]} {year(int(m.group(3)))}" if style == "A"
                          else f"{MONTHS[m.group(2).lower()[:3]]} {ordinal(int(m.group(1)))}, {year(int(m.group(3)))}"), s)
    s = re.sub(r"\b(FY|water year)\s*(\d{4})-(\d{2})\b",
               lambda m: ("F Y" if m.group(1) == "FY" and style == "A" else "financial year" if m.group(1) == "FY" else m.group(1))
               + " " + (f"{year(int(m.group(2)))} {two(int(m.group(3)))}" if style == "A"
                        else f"{year(int(m.group(2)))} to {year(2000 + int(m.group(3)))}"), s)
    s = re.sub(r"\b(\d{4})-(\d{2})\b", lambda m: f"{year(int(m.group(1)))} {two(int(m.group(2)))}" if style == "A"
               else f"{year(int(m.group(1)))} to {year(2000 + int(m.group(2)))}", s)
    s = re.sub(r"\bFY(\d{2})\b", lambda m: ("F Y " if style == "A" else "financial year ") + two(int(m.group(1))), s)
    s = re.sub(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})\b",
               lambda m: f"{m.group(1)} {year(int(m.group(2)))}", s)
    s = re.sub(r"\b(calendar year|in|the) (\d{4})\b", lambda m: f"{m.group(1)} {year(int(m.group(2)))}", s)
    s = re.sub(r"\bQ([1-4])\b", lambda m: f"quarter {ONES[int(m.group(1))]}" if style == "A"
               else f"the {['', 'first', 'second', 'third', 'fourth'][int(m.group(1))]} quarter", s)
    s = re.sub(r"\b([A-Z]{2,6})-U(\d+)\b", lambda m: f"{m.group(1)} unit {two(int(m.group(2)))}", s)
    if style == "B":
        for code, name in NAMES.items():
            s = re.sub(rf"\b{code}'s\b", name + "'s", s)
            s = re.sub(rf"\b{code}\b", name, s)
    for a in LETTERS:
        s = re.sub(rf"\b{a}\b", " ".join(a), s)
    s = re.sub(r"(\d+) MW\b", lambda m: f"{m.group(1)} megawatt", s)
    for a, b in (("%", " percent"), ("&", " and "), ("GWh", "gigawatt hours"), ("MWh", "megawatt hours"), ("kWh", "kilowatt hours"),
                 (" GW", " gigawatts"), ("ESCOM", "escom"), ("*", " star "), ("2 pm", "two P M"), ("3 pm", "three P M")):
        s = s.replace(a, b)
    return re.sub(r"\s{2,}", " ", s).strip()


# ------------------------------------------------------------------ generated bank: every station by its spoken name
SPOKEN_STATION = {
    "RTPS": "Raichur thermal power station", "BTPS": "Bellary thermal power station", "YTPS": "Yermarus thermal",
    "YCCPP": "Yelahanka combined cycle power plant", "BWTEP": "Bidadi waste to energy plant", "SGS": "Sharavati",
    "NPH": "Nagjhari power house", "VUGPH": "Varahi", "MGHE": "Mahatma Gandhi hydro electric station",
    "LPH": "Linganamakki power house", "GERUSOPPA": "Gerusoppa power house", "SUPA": "Supa dam power house",
    "KODASALLI": "Kodasalli", "KADRA": "Kadra dam power house", "MANI": "Mani dam power house",
    "ALAMATTI": "Almatti dam power house", "GHATPRABHA": "Ghataprabha", "BHADRA": "Bhadra generating station",
    "MUNIRABAD": "Munirabad", "SHIVA": "Shiva hydro electric station", "SHIMSHA": "Shimsha", "MALLAPUR": "Mallapur mini hydel",
    "SIRWAR": "Sirwar mini hydel", "KALMALA": "Kalmala mini hydel", "GANEKAL": "Ganekal mini hydel",
    "MALAPRABHA": "Malaprabha mini hydel", "KG-WEF": "Kappatagudda wind farm", "YAL-SOLAR-3": "Yalesandra solar plant",
    "ITN-SOLAR-3": "Itnal solar plant", "YAP-SOLAR-3": "Yapaldinni solar plant",
    "SH-SOLAR-5": "Shivasamudra 5 megawatt solar plant", "SH-SOLAR-10": "Shivasamudra 10 megawatt solar plant",
    "CHANDAPUR": "Chandapur solar plant",
}
FYS = ["2016-17", "2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def generated(conn) -> list[dict]:
    first_fy = dict(conn.execute("SELECT station_key, MIN(fy) FROM mart_fact_unit_generation_daily GROUP BY station_key"))
    out, n = [], 1000
    for i, (key, name) in enumerate(SPOKEN_STATION.items()):
        fys = [f for f in FYS if f > first_fy[key]] or ["2024-25"]
        fy = fys[i % len(fys)]
        n += 1
        out.append({"id": n, "area": "Voice · station name", "q": f"How much did {name} generate in FY {fy}?",
                    "checks": [N(ud(GEN, f"station_key = '{key}' AND fy = '{fy}'"))]})
        y = 2024 if key in ("YCCPP", "BWTEP") else 2017 + i % 8
        mo = (i * 5) % 12 + 1 if key not in ("YCCPP", "BWTEP") else 8 + i % 4
        ym = f"{y}-{mo:02d}"
        n += 1
        out.append({"id": n, "area": "Voice · station name", "q": f"What was the plant load factor of {name} in {MONTH_NAMES[mo - 1]} {y}?",
                    "checks": [N(ud(PLF, f"station_key = '{key}' AND year_month = '{ym}'"))]})
    for st, units in (("RTPS", [1, 3, 5, 8]), ("BTPS", [1, 2, 3]), ("YTPS", [1, 2]), ("SGS", [2, 6, 10]), ("NPH", [4]), ("VUGPH", [3])):
        for u in units:
            n += 1
            mo = (u * 4) % 12 + 1
            y = 2018 + u % 6
            out.append({"id": n, "area": "Voice · unit", "q": f"{st}-U{u} generation in {MONTH_NAMES[mo - 1]} {y}",
                        "checks": [N(ud(GEN, f"unit_key = '{st}-U{u}' AND year_month = '{y}-{mo:02d}'"))]})
    for res, d in (("Linganamakki", "2021-08-15"), ("Supa", "2019-02-11"), ("Talakalale", "2023-05-31"), ("Mani", "2020-10-02"),
                   ("Gerusoppa", "2018-12-25"), ("Alamatti", "2022-07-19")):
        n += 1
        y, m, dd = d.split("-")
        out.append({"id": n, "area": "Voice · reservoir", "q": f"What was the {res} reservoir level on {int(dd)} {MONTH_NAMES[int(m) - 1]} {y}?",
                    "checks": [N(f"SELECT water_level FROM mart_fact_reservoir_daily WHERE reservoir_name = '{res.upper()}' AND date_key = '{d}'")]})
    for sup, code, fy in (("Singareni", "SCCL", "2020-21"), ("Western Coalfields", "WCL", "2016-17"), ("Mahanadi", "MCL", "2023-24"),
                          ("imported coal", "IMPORTED", "2019-20")):
        n += 1
        what = "imported coal" if code == "IMPORTED" else f"coal from {sup}"
        out.append({"id": n, "area": "Voice · coal supplier", "q": f"How much {what} did KPCL receive in FY {fy}?",
                    "checks": [N(f"SELECT SUM(quantity_mt) FROM mart_fact_coal_receipt WHERE supplier_code = '{code}' AND fy = '{fy}'")]})
    for d in ("2016-03-14", "2019-11-02", "2022-06-21", "2024-12-31"):
        n += 1
        y, m, dd = d.split("-")
        out.append({"id": n, "area": "Voice · date", "q": f"How much did KPCL generate on {int(dd)} {MONTH_NAMES[int(m) - 1]} {y}?",
                    "checks": [N(ud(GEN, f"date_key = '{d}'"))]})
    return out


# ------------------------------------------------------------------ running
NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(crore|lakh)?")


def spoken_has(spoken: str, expected: float) -> bool:
    for w, f, scale in NUM.findall(spoken or ""):
        v = float(w.replace(",", "") + ("." + f if f else "")) * {"crore": 1e7, "lakh": 1e5}.get(scale, 1)
        if abs(v - expected) <= max(abs(expected) * 0.006, 0.006 if abs(expected) < 10 else 0.051 if abs(expected) < 100 else 0.51):
            return True
    return False


def make_wavs(trials: list[dict]) -> None:
    WAVS.mkdir(parents=True, exist_ok=True)
    todo = [{"path": t["wav"], "voice": t["voice"], "text": t["said"]} for t in trials if not Path(t["wav"]).exists()]
    if not todo:
        return
    lst = WAVS / "phrases.json"
    lst.write_text(json.dumps(todo), encoding="utf-8")
    print(f"speaking {len(todo)} questions to WAV ...", flush=True)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "make_wavs.ps1"), "-List", str(lst)],
                   check=True)


_GENERIC = {"station", "power", "house", "thermal", "hydro", "electric", "mini", "hydel", "solar", "plant", "reservoir",
            "generating"}


def pick_option(options: list[dict], meant: str) -> int:
    """Which 'Did you mean ...?' option the speaker meant (same word match as the panel's spoken reply)."""
    t = " " + re.sub(r"[^a-z0-9-]+", " ", meant.lower()) + " "
    meant_years = set(re.findall(r"(?:19|20)(\d{2})-(\d{2})", meant))
    def score(o: dict) -> int:
        words = [w for w in re.findall(r"[a-z]{4,}", o["label"].lower()) if w not in _GENERIC]
        years = set(re.findall(r"(?:19|20)(\d{2})-(\d{2})", o["label"]))
        return sum(f" {w} " in t for w in words) + 2 * len(years & meant_years)
    scores = [score(o) for o in options]
    return scores.index(max(scores))


async def run_trial(client, conn, t: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        t0 = time.time()
        audio = Path(t["wav"]).read_bytes()
        r = await client.post(f"/api/voice/transcribe?session_id=t{t['item']['id']}{t['style']}", content=audio,
                              headers={"content-type": "audio/wav"})
        heard = r.json().get("text", "") if r.status_code == 200 else f"!!! transcribe {r.status_code}"
        conf = (r.json().get("confirm") or {}) if r.status_code == 200 else {}
        confirm, question = conf.get("prompt", ""), heard
        if conf.get("options"):                       # the officer taps the option they meant, as on the voice panel
            i = pick_option(conf["options"], t["item"]["q"])
            question = conf["options"][i]["question"]
            confirm += f" -> {conf['options'][i]['label']}"
        a = await client.post("/api/ask_once", json={"question": question or "(silence)", "mode": "voice"})
        ans = a.json() if a.headers.get("content-type", "").startswith("application/json") else {"answer": a.text}
        if a.status_code >= 400:
            ans = {"answer": ans.get("detail", ""), "answerable": False}
        problems, expected = check(t["item"], ans, conn)
        num_checks = [c for c in t["item"]["checks"] if c[0] == "num" and not c[2]]
        if not problems and len(num_checks) == 1 and len(ans.get("rows") or []) <= 1:
            row = conn.execute(num_checks[0][1]).fetchone()
            if row and len(row) == 1 and row[0] is not None and ans.get("spoken_answer") and not spoken_has(ans["spoken_answer"], float(row[0])):
                problems.append(f"spoken answer lacks {row[0]:,.3f}")
        out = {"id": t["item"]["id"], "style": t["style"], "voice": t["voice"], "area": t["item"]["area"], "q": t["item"]["q"],
               "said": t["said"], "heard": heard, "expected": expected, "problems": problems,
               "answer": (ans.get("answer") or "")[:400], "spoken": ans.get("spoken_answer", ""),
               "confirm": confirm, "status": "PASS" if not problems else "FAIL", "secs": round(time.time() - t0, 2)}
        if problems:                                  # is it the voice layer or the pipeline?
            typed = (await client.post("/api/ask_once", json={"question": t["item"]["q"]})).json()
            tp, _ = check(t["item"], typed, conn)
            out["typed_status"] = "PASS" if not tp else "FAIL"
        print(f"{out['status']} {out['id']}{t['style']} [{out['area']}] heard: {heard[:110]}" + (f"  [confirmed: {confirm}]" if confirm else "")
              + ("" if not problems else f"  <-- {problems} (typed: {out['typed_status']})"), flush=True)
        return out


async def main_async(a) -> None:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    items = [b for b in BANK if b["id"] not in UNSPEAKABLE] + generated(conn)
    if a.only:
        keep = {int(x) for x in a.only.split(",")}
        items = [b for b in items if b["id"] in keep]
    prev = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {"results": {}}
    trials = []
    for b in items:
        for style, voice in (("A", "Ravi"), ("B", "Heera")):
            key = f"{b['id']}{style}"
            if a.failed and prev["results"].get(key, {}).get("status") == "PASS":
                continue
            said = speakify(b["q"], style)
            trials.append({"item": b, "style": style, "voice": voice, "said": said,
                           "wav": str(WAVS / f"{key}_{hashlib.md5(said.encode()).hexdigest()[:10]}.wav")})
    if a.limit:
        trials = trials[: a.limit]
    make_wavs(trials)
    from app.main import app
    sem = asyncio.Semaphore(a.workers)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice", timeout=300) as client:
            results = await asyncio.gather(*(run_trial(client, conn, t, sem) for t in trials))
    merged = prev["results"] if (a.failed or a.only) else {}
    merged.update({f"{r['id']}{r['style']}": r for r in results})
    passed = sum(r["status"] == "PASS" for r in merged.values())
    REPORT.write_text(json.dumps({"passed": passed, "total": len(merged), "results": merged}, indent=1, ensure_ascii=False),
                      encoding="utf-8")
    write_md(merged)
    print(f"\n{passed}/{len(merged)} spoken questions passed (this run: {sum(r['status'] == 'PASS' for r in results)}/{len(results)})")


def write_md(res: dict) -> None:
    rows = sorted(res.values(), key=lambda r: (r["id"], r["style"]))
    passed = sum(r["status"] == "PASS" for r in rows)
    lines = ["# Voice stress test (spoken questions)", "",
             f"**{passed} of {len(rows)} spoken questions answered correctly** ({time.strftime('%d %B %Y')}). Each question was "
             "spoken by a Windows Indian-English voice (A: Ravi, codes letter by letter; B: Heera, station names and long-form "
             "years), heard by the real speech-to-text, cleaned up and answered by the voice pipeline. Expected figures are "
             "recomputed from the warehouse; a single figure must also be in what the voice says. "
             "Re-run: `python tests/run_voice_stress.py` in `webapp/kpcl-voice-app` (VPN needed).", "",
             "| # | Area | Spoken | Heard | Expected | Result |", "|---|---|---|---|---|---|"]
    for r in rows:
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
        res_txt = r["status"] if r["status"] == "PASS" else f"FAIL: {'; '.join(r['problems'])} (typed: {r.get('typed_status', '?')})"
        lines.append(f"| {r['id']}{r['style']} | {cell(r['area'])} | {cell(r['said'])} | {cell(r['heard'])} | "
                     f"{cell(', '.join(r['expected']))} | {cell(res_txt)} |")
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--failed", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    asyncio.run(main_async(ap.parse_args()))
