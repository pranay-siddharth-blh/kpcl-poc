"""
What the voice says: a short spoken form of an answer the pipeline already wrote and verified. No model call.

Why deterministic (not a model rewrite, as in the Meghalaya agent): every KPCL answer is written by reviewed code from
the query result, with a bold headline that already states the answer. Re-wording it with a model would add a second
round trip (1-3 s of silence) and a chance of a wrong spoken figure (Meghalaya's model once said "3.06 lakh" for
30.6 lakh and needed a number guard). Here every spoken figure is the screen figure, rounded the way an officer says it.

  spoken_answer(question, result)   -> 1-2 sentences for the ear (also the caption in the voice panel)
  pronounce(text)                   -> the same text made speakable for TTS: station codes spelled out ("R T P S"),
                                       units in words, FY / dates / months read naturally

Rules: the answer first (the headline); at most one supporting sentence; at most four figures; lists cut to three
items; screen-only notes ("in the table below", "pinned to the K-eRP report") are not read; a long answer ends with
"The full details are on your screen."
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from functools import lru_cache

MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June",
          "jul": "July", "aug": "August", "sep": "September", "oct": "October", "nov": "November", "dec": "December"}

IDENTITY = ("I'm the KPCL generation data assistant. Ask me about any station or unit: generation, plant load factor, "
            "outages, coal, reservoirs or targets, from April 2015 to April 2025.")
SCREEN_NOTE = "The full details are on your screen."
REPORT_NOTE = "The full details, and the report, are on your screen."
FILLERS = ["Let me check that.", "One moment, I'm pulling up the figures.", "Checking the records now."]

# Sentences that only make sense on screen.
_SCREEN_ONLY = re.compile(r"\b(table below|in the table|below\b|pinned to|chart|scroll|click|download|CSV|"
                          r"'now' means|data ends on|this period is incomplete|period is reported by)", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9'\"(])")
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w])")


def _plain(text: str) -> str:
    """Markdown and list marks out; whitespace collapsed."""
    t = re.sub(r"\*\*|__|`", "", text or "")
    t = re.sub(r"^\s*[•\-*]\s+", "", t, flags=re.M)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


def _dates(text: str) -> str:
    """06-Apr-2025 -> 6 April 2025;  2018-03-01 -> 1 March 2018;  MAR-2025 / Mar 2025 -> March 2025."""
    text = re.sub(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b",
                  lambda m: f"{int(m.group(3))} {list(dict.fromkeys(MONTHS.values()))[int(m.group(2)) - 1]} {m.group(1)}"
                  if 1 <= int(m.group(2)) <= 12 else m.group(0), text)
    text = re.sub(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b",
                  lambda m: f"{int(m.group(1))} {MONTHS.get(m.group(2).lower(), m.group(2))} {m.group(3)}", text)
    return re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[- ](\d{4})\b",
                  lambda m: f"{MONTHS[m.group(1).lower()]} {m.group(2)}", text, flags=re.I)


def _round_value(num: str, unit: str) -> str:
    """A screen figure as an officer would say it. Never changes more than the last decimals."""
    v = float(num.replace(",", ""))
    u = unit.lower()
    if u in ("t", "tonnes", "tonne"):
        if v >= 1e7:
            return f"about {v / 1e7:,.2f} crore tonnes"
        if v >= 1e6:
            return f"about {v / 1e5:,.1f} lakh tonnes"
        if v >= 1e5:
            return f"about {v / 1e5:,.2f} lakh tonnes"
        return f"{v:,.0f} tonnes"
    if u in ("%", "percent", "points"):
        return (f"{v:,.2f}".rstrip("0").rstrip(".")) + (" percent" if u != "points" else " points")
    if u == "mu":
        s = f"{v:,.0f}" if v >= 1000 else f"{v:,.1f}" if v >= 100 else f"{v:,.2f}".rstrip("0").rstrip(".")
        return f"{s} million units"
    if v >= 1000:
        return f"{v:,.0f} {unit}".strip()
    if u == "days":
        return f"{v:,.0f} days" if v == int(v) else f"{v:,.1f} days"
    return (f"{v:,.2f}".rstrip("0").rstrip(".") + f" {unit}").strip()


_FIG = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(MU|%|tonnes|t\b|points|kcal/kWh|kcal/kg|kg/kWh|"
                  r"ml/kWh|GWh|TWh|MWh|kWh|MW\b|feet|metres|m\b|days|MCFT|KL)?", re.I)
_UNIT_WORDS_FIG = {"kcal/kwh": "kilocalories per kilowatt-hour", "kcal/kg": "kilocalories per kilogram",
                   "kg/kwh": "kilograms per kilowatt-hour", "ml/kwh": "millilitres per kilowatt-hour",
                   "mw": "megawatts", "m": "metres", "mcft": "million cubic feet", "kl": "kilolitres",
                   "gwh": "gigawatt-hours", "twh": "terawatt-hours", "mwh": "megawatt-hours", "kwh": "kilowatt-hours"}


def _speak_figures(text: str) -> str:
    def sub(m):
        whole, frac, unit = m.group(1), m.group(2), m.group(3)
        if not unit or re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|"
                                 r"November|December)\s*$", m.string[:m.start()]):
            return m.group(0)
        out = _round_value(whole + ("." + frac if frac else ""), unit)
        u = unit.lower()
        if u in _UNIT_WORDS_FIG:
            out = out[: -len(unit)].rstrip() + " " + _UNIT_WORDS_FIG[u]
        return out
    return _FIG.sub(sub, text)


def _parentheses(text: str) -> str:
    """'(PLF)', '(MU)' are dropped; 'highest in March 2025 (3,905 MU)' -> 'highest in March 2025, at 3,905 MU';
    '(31 March 2025)' -> ', 31 March 2025,'; anything else becomes a comma clause."""
    text = re.sub(r"\s*\((?:[A-Z][A-Za-z]{0,6}|%|t|MU|MW|[A-Za-z]+/[A-Za-z]+)\)", "", text)
    text = re.sub(r"\s*\((\d[\d,.]*\s*(?:GWh|TWh|MWh|kWh|lakh|crore|billion)\b[^()]*)\)", r", or \1,", text)
    text = re.sub(r"\s*\((\d[\d,.]*\s*(?:MU|%|tonnes|t|MW|GWh|feet|kcal/kWh)\b[^()]*)\)", r", at \1,", text)
    return re.sub(r"\s*\(([^()]*)\)", r", \1,", text)


def _clean(sentence: str) -> str:
    s = _dates(_plain(sentence))
    s = re.sub(r"\s+[—–]\s+[^:,.;]{1,40}:\s*", ", ", s)            # "Supa Dam — PLF %: 73.83%" -> "Supa Dam, 73.83%"
    s = _parentheses(s)
    s = re.sub(r"'([A-Z][\w-]*)'", r"\1", s)                     # 'ZZTPS' -> ZZTPS
    s = _speak_figures(s)
    s = re.sub(r"(\d[\d,]*\.\d*?)0*\s*MMSCM\s+(million standard cubic metres)", r"\1 \2", s)   # "155.5200 MMSCM million ..."
    s = re.sub(r"(\d)\.\s(million standard)", r"\1 \2", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = re.sub(r",\s*(?=[,.;:!?])", "", s)                       # ", ." / ", ," left by the bracket rules
    s = re.sub(r"\s{2,}", " ", s).strip().rstrip(",;:")
    if s and s[-1] not in ".!?":
        s += "."
    return s


def _paragraphs(answer: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", answer or "") if p.strip()]


def _bullets(answer: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*[•\-]\s+(.+)$", answer or "", re.M)]


def _headline(answer: str) -> str:
    m = re.match(r"\s*\*\*(.+?)\*\*", answer or "", re.S)
    return m.group(1).strip() if m else ""


def _years_out(text: str) -> str:
    return re.sub(r"\b(?:FY ?)?(?:19|20)\d{2}(?:-\d{2})?\b|\b\d{1,2}[- ](?:[A-Za-z]{3,9})[- ]\d{4}\b", " ", text)


def _numbers(text: str) -> set[str]:
    return {a + b for a, b in _NUM.findall(_years_out(text))}


def _list_sentence(items: list[str], head: str) -> str:
    """Up to three list items; if the headline already named the first one, continue with 'followed by'."""
    # one clause per item for the ear: "RTPS-U1: forced outage since 10-Aug-2022 (995 days)" -> "... since 10-Aug-2022"
    items = [re.sub(r"\s*\([^)]*\)\s*$", "", _plain(i).split(" · ")[0]).rstrip(".") for i in items]
    first_name = re.split(r"\s*[—–:]\s*|,", items[0])[0].strip()
    lead = ""
    if first_name and first_name.lower() in head.lower() and len(items) > 1:
        items, lead = items[1:3], "Followed by "
    else:
        items = items[:3]
    items = [re.sub(r"\s*[—–]\s*[^:]{1,40}:\s*", " at ", i) for i in items]
    items = [re.sub(r":\s*", ", ", i) for i in items]
    body = items[0] if len(items) == 1 else ", ".join(items[:-1]) + (" and " if lead else ", and ") + items[-1]
    return lead + body


_HEADLINE_ONLY = ("capability", "out_of_range", "unknown_unit", "llm_unavailable", "llm_error", "llm_declined",
                  "llm_sql_failed", "llm_mismatch", "llm_unverified")


def spoken_answer(question: str, answer: str, route: str, *, rows: int = 0, has_chart: bool = False,
                  has_report: bool = False) -> str:
    """1-2 sentences to say for `answer` (already verified and shown on screen)."""
    if route == "identity":
        return IDENTITY
    if not answer:
        return "Sorry, I couldn't answer that just now. Please ask again."
    head = _headline(answer)
    rest = re.sub(r"^\*\*\s*", "", answer[answer.find(head) + len(head):]) if head else answer
    if route == "glossary":                                       # "**PAF**: Plant Availability Factor: ..."
        term, _, defn = _plain(answer).partition(":")
        first = _sentences(defn.strip())[0] if defn.strip() else term
        return _clean(f"{term.strip()} stands for {first}")
    if route in ("llm_unavailable", "llm_error"):
        return ("That question needs the language model, which isn't reachable right now. Single figures, "
                "definitions and the standard reports still work, so please try one of those, or ask again shortly.")
    if route == "unknown_name":
        return _clean(head) + " Please say the station name again, for example R T P S or Sharavathy."
    is_sentence = bool(head) and head[-1] in ".!?"
    spoken: list[str] = []
    if head and head.endswith(":"):                              # "KPCL in FY 2023-24 at a glance:" + bullets
        spoken.append(_clean(head.rstrip(":") + "."))
        spoken += [_clean(b) for b in _bullets(answer)[:2]]
    elif is_sentence:
        spoken.append(_clean(head))
    else:                                                        # a title (knowledge) or no bold headline
        paras = [p for p in _paragraphs(rest if head else answer) if not p.lstrip().startswith(("•", "-"))]
        sents = [s for p in paras for s in _sentences(_plain(p)) if not _SCREEN_ONLY.search(s)]
        spoken += [_clean(s) for s in sents[:2]]
    if route.startswith(_HEADLINE_ONLY) or route.startswith("refused"):
        spoken = spoken[:1] if is_sentence else spoken[:2]
        if route == "capability":
            spoken.append("I can tell you about generation, plant load factor, outages, coal, reservoirs and targets.")
    elif len(spoken) == 1 and is_sentence and len(_numbers(head)) < 3:
        # One supporting sentence (or up to three list items) that adds a figure the headline doesn't have.
        bl = _bullets(answer)
        head_nums = _numbers(head)
        names = [re.split(r"\s*[—–:·]\s*|,", _plain(b))[0].strip().lower() for b in bl]
        already = sum(1 for n in names[:3] if n and n in head.lower())
        busy_items = any(len(_numbers(_plain(b).split(" · ")[0])) > 2 for b in bl[:3])
        if bl and 2 <= len(bl) <= 12 and already < 2 and not busy_items and not _SCREEN_ONLY.search(bl[0]):
            spoken.append(_clean(_list_sentence(bl, head)))
        else:
            for p in _paragraphs(rest):
                if p.lstrip().startswith(("•", "-")):
                    continue
                for s in _sentences(_plain(p)):
                    nums = _numbers(s)
                    if _SCREEN_ONLY.search(s) or not nums or nums & head_nums or len(nums) > 4:
                        continue
                    spoken.append(_clean(s))
                    break
                if len(spoken) > 1:
                    break
    text = re.sub(r"\s*\n\s*", ". ", " ".join(spoken))
    text = re.sub(r"\.\s*\.", ".", text)
    if len(text.split()) > 70:                                   # keep it to what the ear can hold
        text = spoken[0]
    more_on_screen = rows > 1 or has_chart or (route in ("knowledge", "knowledge_fallback")
                                               and len(_plain(answer).split()) > len(text.split()) + 25)
    if more_on_screen:
        text += " " + (REPORT_NOTE if has_report else SCREEN_NOTE)
    return text.strip()


# ---------------------------------------------------------------------------------------------- transcripts
# What the clean-up was unsure about, for the "Did you mean ...?" confirmation (app/voice_guard.confirmation).
# Each note: {"kind": "name" | "year", "span": text in the cleaned question, "options": [most likely, other]}.
_NOTES: ContextVar[list | None] = ContextVar("voice_clean_notes", default=None)


def _note(kind: str, span: str, options: list[str]) -> None:
    notes = _NOTES.get()
    if notes is not None and len({o.lower() for o in options}) > 1:
        notes.append({"kind": kind, "span": span, "options": options})


_SPELLED = re.compile(r"\b(?:[A-Za-z][.\s]\s*){1,5}[A-Za-z]\b\.?")
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                 "nine": "9", "ten": "10"}


_W1 = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                                    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                                    "eighteen", "nineteen"])}
_W10 = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_TWO = r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|" \
       r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"


def _two_digits(words: str) -> int:
    parts = re.split(r"[\s-]+", words.lower().strip())
    return sum(_W10.get(p, 0) + _W1.get(p, 0) for p in parts)


def _spoken_years(t: str) -> str:
    """'two thousand and twenty three twenty four' / 'twenty twenty-three, twenty-four' / '2023 24' -> '2023-24'."""
    t = re.sub(rf"\btwo thousand(?: and)?[\s-]+({_TWO})\b", lambda m: str(2000 + _two_digits(m.group(1))), t, flags=re.I)
    t = re.sub(rf"\btwenty[\s-]+({_TWO})\b", lambda m: str(2000 + _two_digits(m.group(1))), t, flags=re.I)
    t = re.sub(rf"\b(20\d\d)[\s,]*(?:to|-|and)?[\s,]*({_TWO})\b", lambda m: f"{m.group(1)}-{_two_digits(m.group(2)):02d}", t,
               flags=re.I)
    t = re.sub(r"\b(20\d\d)\s*(?:to|-|/)?\s*(20)?(\d\d)\b(?!-)",
               lambda m: f"{m.group(1)}-{m.group(3)}" if int(m.group(3)) == (int(m.group(1)) + 1) % 100 else m.group(0), t)
    return t


def _phon(word: str) -> str:
    """A rough sound key for Indian place names as speech-to-text spells them: Sharavati / Sharavathi / Sharavathy,
    Nagzari / Nagjhari, Yermaras / Yeramarus, Ghataprabha / Ghatprabha all collapse to near-identical keys."""
    s = word.lower()
    for a, b in (("th", "t"), ("dh", "d"), ("bh", "b"), ("gh", "g"), ("kh", "k"), ("sh", "s"), ("ph", "f"),
                 ("ck", "k"), ("w", "v"), ("z", "j"), ("ee", "i"), ("oo", "u"), ("y", "i"), ("q", "k")):
        s = s.replace(a, b)
    s = s.replace("h", "")
    return re.sub(r"(.)\1+", r"\1", s)


# Words that are never station names, so they are never "corrected" (the question's own vocabulary).
_ORDINARY = set("""what was the how much many did does do in of for and to from by at on with which who when where is are
were generate generated generation gross net plf paf load factor plant availability station stations unit units power
house thermal hydro electric dam generating mini hydel solar wind farm energy waste combined cycle underground company
ltd coal stock receipt receipts received burn burned burnt imported foreign import western reservoir level storage
water year financial fiscal month months day days week weeks quarter season summer monsoon winter total compare
between versus best worst highest lowest top bottom trend monthly weekly daily yearly annual average report summary
overview performance outage outages trips trip forced planned reason reasons target targets achievement auxiliary
consumption heat rate specific oil spill inflow rainfall discharge grid state demand peak share karnataka kpcl
india indian april march february january may june july august september october november december now right
today latest last current currently please tell show give me list all every each about running down out service
attention risk situation position fullest emptiest major minor category categories gas mw mu gwh percent tonnes
lakh crore million units capacity installed commissioned since during against previous next first""".split())


@lru_cache(maxsize=1)
def _name_vocab() -> dict[str, str]:
    """Sound key -> the spelling the router knows, for every word of every station / reservoir / supplier alias."""
    from app.semantic_layer import router_index
    ix = router_index()
    out: dict[str, str] = {}
    for aliases in list(ix.stations.values()) + list(ix.reservoirs.values()) + list(ix.suppliers.values()):
        for alias in aliases:
            for w in re.findall(r"[a-z]+", alias.lower()):
                if len(w) >= 4 and w not in _ORDINARY:
                    out.setdefault(_phon(w), w)
    return out


def _fix_names(t: str) -> str:
    """Respell place names the way the data knows them: 'Sharavati' -> 'sharavathi', 'Linga namakki' ->
    'linganamakki'. Exact sound-key match for any word; a close match (similarity >= 0.86) only for words of 6+
    letters that are not ordinary question words, so 'generate' or 'station' are never touched."""
    import difflib
    vocab = _name_vocab()
    known = set(vocab.values())
    words = re.findall(r"[A-Za-z]+|[^A-Za-z]+", t)
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if not w.isalpha() or len(w) < 4 or w.lower() in _ORDINARY or w.lower() in known:
            out.append(w)
            i += 1
            continue
        # a name split in two by the transcriber: "Linga namakki", "Kappata gudda"
        if i + 2 < len(words) and words[i + 1] == " " and words[i + 2].isalpha():
            joined = _phon(w + words[i + 2])
            if joined in vocab and (w + words[i + 2]).lower() not in _ORDINARY:
                out.append(vocab[joined])
                i += 3
                continue
        key = _phon(w)
        if key in vocab:
            out.append(vocab[key])
        elif len(w) >= 6:
            ranked = sorted(vocab, key=lambda k: difflib.SequenceMatcher(None, key, k).ratio(), reverse=True)
            best = ranked[0]
            if difflib.SequenceMatcher(None, key, best).ratio() >= 0.86:
                out.append(vocab[best])
                other = next((vocab[k] for k in ranked[1:] if _place_of(vocab[k]) != _place_of(vocab[best])), None)
                if other and difflib.SequenceMatcher(None, key, best).ratio() < 0.95:
                    _note("name", vocab[best], [vocab[best], other])
            else:
                out.append(w)
        else:
            out.append(w)
        i += 1
    return "".join(out)


def _place_of(word: str) -> str:
    from app.voice_guard import place_label
    return place_label(word)


def _repair_merged_years(t: str) -> str:
    """Speech-to-text merges "twenty twenty-two twenty-three" into "2020 to 23" / "2020-2023" / "2020-23", and
    "twenty twenty-one twenty-two" into "2020 even 22". The second year survives, so the FY is (second - 1)-(second).
    Applied only right after FY / financial year / fiscal year / water year, or with the tell-tale "even" (a real
    range such as "from 2016 to 2018" is never touched)."""
    def fy(prefix: str, b: int) -> str:
        chosen = f"20{b - 1:02d}-{b:02d}"
        _note("year", chosen, [chosen, "2020-21"])          # the first year may have been right after all
        return f"{prefix}{chosen}"
    t = re.sub(r"\b((?:FY|financial year|fiscal year|water year)\s*)2020\s*(?:to|-|–|even|and)\s*(?:20)?(2[2-6])\b(?!-)",
               lambda m: fy(m.group(1), int(m.group(2))), t, flags=re.I)
    return re.sub(r"\b2020\s+even\s+(?:20)?(2[1-6])\b", lambda m: fy("", int(m.group(1))), t, flags=re.I)


# Mis-hearings of ordinary question words seen in the voice stress test (Indian-English voices, qwen3-asr).
_PHRASES = [
    (r"\bpower\s?house\b|\bpowerhouse\b", "power house"),
    (r"\bmini\s+(?:huddle|haddle|haddal|hetal|havel|hydle|hiddle|headle|hydro)\b", "mini hydel"),
    (r"\b(?:wine|vine|wing)\s+farm", "wind farm"),
    (r"\bsharp\s?(?:in|pin)\s+failure", "shear pin failure"),
    (r"\brunning\s+(?:oz|ours|owls|hours)\b", "running hours"),
    (r"\boutage\s+(?:as|ours|owers)\b", "outage hours"),
    (r"\bdes\s+of\s+coal\b", "days of coal"),
    (r"\b(\d+)\s*mega\s?watts?\b", r"\1 MW"),
    (r"\bquarter\s+(one|two|three|four|[1-4])\b", lambda m: "Q" + {"one": "1", "two": "2", "three": "3", "four": "4"}.get(m.group(1).lower(), m.group(1))),
    (r"\b(?:the\s+)?(first|second|third|fourth)\s+quarter\b", lambda m: "Q" + str(["first", "second", "third", "fourth"].index(m.group(1).lower()) + 1)),
    (r"\btunga\s?(?:vadra|badra|bhadra|vadhra)\b", "Tungabhadra"),
    (r"\b(?:we|wee|v)\s?UGPH\b", "VUGPH"),
    (r"\bmani\s?rabad\b", "Munirabad"),
    (r"\blinganmakki\s+power house\b", "Linganamakki power house"),
    (r"\bmoney\s+(dam|reservoir|power house)\b", r"Mani \1"),
    (r"\b(?:cyber|siber|sarvar)\s+mini\b", "Sirwar mini"),
    (r"\b(?:mini\s+)?(?:hetal|haddle|huddle|haddal|havel|hidel)\b", "mini hydel"),
    (r"\b(?:shiva|siva|sivagiri|shivagiri)\s?mini\b", "Sirwar mini"),        # Shiva is not a mini hydel; Sirwar is
    (r"\b(?:shiva|siva)\s?s(?:a|u)?mudr(?:a|am)\b", "Shivasamudra"),
    (r"\boutage\s+(?:was|as|ours)\s+(?=at|of|in|for)\b", "outage hours "),
    (r"\bunit\s+(?:for|fore)\b(?=\s+(?:generation|plf|paf|in|on|for|output|trips))", "unit 4"),
    (r"\bunit\s+(?:to|too)\b(?=\s+(?:generation|plf|paf|in|on|output|trips))", "unit 2"),
    (r"\b(financial|fiscal)\s+year\s+(\d{2})\b(?![-\d])", r"FY\2"),
    (r"\b(?:financial|fiscal)\s+year\s+(20\d{2})\b(?![-\d])", r"FY \1"),
]
# Station / reservoir names as speech-to-text spells them -> the name the data knows: app/voice_lexicon.json,
# validated at load by app/voice_guard.lexicon() (an entry can never turn one real station into another).
def _lexicon() -> dict[str, str]:
    from app.voice_guard import lexicon
    return lexicon()["mishearings"]


_ORDINAL_DAYS = {w: i for i, w in enumerate(
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
_ORDINAL_DAYS.update({"thirtieth": 30})
_MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
_ORD_WORD = r"((?:twenty|thirty)[\s-]?)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|" \
            r"thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|thirtieth|\d{1,2}(?:st|nd|rd|th))"


def _ordinal_day(tens: str | None, word: str) -> int:
    if word[:1].isdigit():
        return int(re.sub(r"\D", "", word))
    return (20 if tens and tens.lower().startswith("twenty") else 30 if tens else 0) + _ORDINAL_DAYS[word.lower()]


def _spoken_dates(t: str) -> str:
    """'twenty sixth January 2020' / 'January twenty-sixth, 2020' / 'June 1st 2023' -> '26 January 2020'."""
    t = re.sub(rf"\b{_ORD_WORD}\s+(?:of\s+)?{_MONTH_RE},?\s+(\d{{4}})\b",
               lambda m: f"{_ordinal_day(m.group(1), m.group(2))} {m.group(3)} {m.group(4)}", t, flags=re.I)
    return re.sub(rf"\b{_MONTH_RE}\s+(?:the\s+)?{_ORD_WORD},?\s+(\d{{4}})\b",
                  lambda m: f"{_ordinal_day(m.group(2), m.group(3))} {m.group(1)} {m.group(4)}", t, flags=re.I)


@lru_cache(maxsize=1)
def _anchor_names() -> list[tuple[re.Pattern, list[str]]]:
    """(anchor phrase, the station / reservoir names that can stand right before it), from the data's own names:
    "X mini hydel" can only be a mini hydel, "X thermal" only a thermal station, "X reservoir" only a reservoir."""
    from app.semantic_layer import load_semantic_layer, router_index
    stations = load_semantic_layer()["entities"]["stations"]
    first = lambda name: name.split()[0]  # noqa: E731
    groups = {
        r"mini hydel": [first(s["name"]) for s in stations if "Mini Hydel" in s["name"]],
        r"thermal": ["Raichur", "Bellary", "Ballari", "Yeramarus", "Yermarus"],
        r"solar": [first(s["name"]) for s in stations if "Solar" in s["name"]],
        r"(?:dam\s+)?power house|dam": [first(s["name"]) for s in stations if "Power House" in s["name"]] + ["Mani", "Supa", "Kadra"],
        r"reservoir|dam": [n.title() for n in router_index().reservoirs],
        r"wind farm": ["Kappatagudda"],
    }
    return [(re.compile(rf"\b([A-Za-z]+(?:\s[A-Za-z]+)?)\s+(?={a}\b)", re.I), sorted(set(n))) for a, n in groups.items()]


def _anchored(t: str) -> str:
    """A word right before an anchor ("mini hydel", "thermal", "solar", "power house", "dam", "reservoir", "wind farm")
    is matched only against names of that kind, so a looser sound match is safe: "Cyber mini hydel" -> Sirwar,
    "Raju thermal" -> Raichur. Names that already match exactly, and ordinary words, are left alone."""
    import difflib
    for pat, names in _anchor_names():
        keys = {_phon(n): n for n in names}

        def score(word):
            k = _phon(word)
            best = max(keys, key=lambda x: difflib.SequenceMatcher(None, k, x).ratio())
            return best, difflib.SequenceMatcher(None, k, best).ratio()

        def sub(m):
            words = m.group(1).split()
            last = words[-1]
            if last.lower() in _ORDINARY or any(last.lower() == n.lower() for n in names):
                return m.group(0)
            lead = words[0] + " " if len(words) == 2 else ""
            # a name split in two ("Shiva Smudra"): both words unusual and a strong joint match
            if len(words) == 2 and words[0].lower() not in _ORDINARY:
                best, r = score(words[0] + last)
                if r >= 0.8:
                    return keys[best] + " "
            best, r = score(last)
            if r < 0.6:
                return m.group(0)
            if r < 0.8:
                k = _phon(last)
                others = sorted((x for x in keys if x != best), key=lambda x: difflib.SequenceMatcher(None, k, x).ratio(),
                                reverse=True)
                if others:
                    _note("name", keys[best], [keys[best], keys[others[0]]])
            return lead + keys[best] + " "
        t = pat.sub(sub, t)
    return t


def _confusable_and_years(t: str) -> None:
    """Notes for a known confusable name heard on its own (Shiva / Sirwar) and a single ambiguous year (FY 2024)."""
    from app.voice_guard import lexicon
    for c in lexicon()["confusable"]:
        unless = "|".join(map(re.escape, c.get("unless_followed_by", [])))
        for h in c["heard"]:
            m = re.search(rf"\b{re.escape(h)}\b(?!\s+(?:{unless})\b)" if unless else rf"\b{re.escape(h)}\b", t, re.I)
            if m:
                _note("name", m.group(0), list(c["options"]))
                return
    m = re.search(r"\bFY\s?(?:20)?(\d{2})\b(?![-\d])", t)
    if m and 16 <= int(m.group(1)) <= 26:
        yy = int(m.group(1))
        _note("year", m.group(0), [f"FY 20{yy - 1:02d}-{yy:02d}", f"FY 20{yy:02d}-{yy + 1:02d}"])


def clean_transcript_ex(text: str) -> tuple[str, list[dict]]:
    """clean_transcript plus what it was unsure about (see _note)."""
    token = _NOTES.set([])
    try:
        cleaned = clean_transcript(text)
        _confusable_and_years(cleaned)
        notes = _NOTES.get() or []
    finally:
        _NOTES.reset(token)
    return cleaned, notes


def clean_transcript(text: str) -> str:
    """Speech-to-text output -> the question the pipeline expects. 'R.T.P.S.' / 'r t p s' -> 'RTPS' when that is
    a station code or a known acronym; 'unit eight' -> 'unit 8'; spoken financial years -> '2023-24'; misheard
    station names and words -> the data's names. Anything else is left as heard."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _spoken_years(t)
    t = _repair_merged_years(t)
    t = _spoken_dates(t)
    for pat, rep in _PHRASES:
        t = re.sub(pat, rep, t, flags=re.I)
    lex = _lexicon()
    t = re.sub(r"\b[A-Za-z]+\b", lambda m: lex.get(m.group(0).lower(), m.group(0)), t)
    t = _anchored(t)
    # "FY18-19", "FY 18 19", "financial year 18-19" -> "FY 2018-19" (only consecutive years)
    t = re.sub(r"\b(F\.?\s?Y\.?|financial year|fiscal year)\s*'?(\d{2})\s*(?:-|to|/|\s)\s*(\d{2})\b(?!-)",
               lambda m: (f"FY 20{m.group(2)}-{m.group(3)}" if int(m.group(3)) == (int(m.group(2)) + 1) % 100
                          else m.group(0)), t, flags=re.I)
    t = re.sub(r"\bF\.\s?Y\.?(?=\s|$)", "FY", t)
    t = _fix_names(t)
    known = set(_station_codes()) | set(_ACRONYMS) | {"KPCL", "PLF", "PAF", "CEA", "MU", "FY"}

    def join(m):
        letters = re.sub(r"[^A-Za-z]", "", m.group(0)).upper()
        return letters if letters in known and len(letters) >= 2 else m.group(0)
    t = _SPELLED.sub(join, t)
    t = re.sub(r"\bunit\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b",
               lambda m: "unit " + _NUMBER_WORDS[m.group(1).lower()], t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip()


# ---------------------------------------------------------------------------------------------- pronunciation
_ACRONYMS = ["KPCL", "RPCL", "KPCGPCL", "PLF", "PAF", "NLD", "RSD", "BTF", "BTL", "GCV", "HFO", "LDO", "CEA", "KERC",
             "KPTCL", "SLDC", "MIS", "MPR", "JMR", "HO", "CSR", "RTI", "CIN", "WTE", "RES", "UCB",
             "MW", "GW", "kWh", "MWh", "GWh"]
_UNIT_WORDS = {"MW": "megawatts", "GW": "gigawatts", "kWh": "kilowatt-hours", "MWh": "megawatt-hours",
               "GWh": "gigawatt-hours", "MU": "million units"}
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _year(y: int) -> str:
    """2024 -> twenty twenty-four; 2005 -> two thousand five."""
    if 2000 <= y <= 2009:
        return "two thousand" + (f" {_ONES[y - 2000]}" if y > 2000 else "")
    return f"{_two(y // 100)} {_two(y % 100)}"


def _spell(code: str) -> str:
    return " ".join(code)


@lru_cache(maxsize=1)
def _station_codes() -> dict[str, str]:
    """Station key -> how to say it: acronyms spelled out (RTPS -> R T P S), names as words (SUPA -> Supa)."""
    from app.semantic_layer import load_semantic_layer
    out = {}
    for s in load_semantic_layer()["entities"]["stations"]:
        key = s["key"]
        parts = []
        for part in key.split("-"):
            vowels = sum(c in "AEIOU" for c in part)
            if part.isdigit():
                parts.append(part)
            elif part in ("SOLAR",):
                parts.append("solar")
            elif len(part) <= 5 and vowels / len(part) < 0.35:
                parts.append(_spell(part))
            else:
                parts.append(part.title())
        out[key] = " ".join(parts)
    return out


def pronounce(text: str) -> str:
    """Make spoken text TTS-safe. Applied by /api/voice/speak* just before synthesis."""
    t = text
    t = re.sub(r"\bFY\s*(\d{4})-(\d{2})\b",
               lambda m: f"financial year {_year(int(m.group(1)))}, {_two(int(m.group(2)))}", t)
    t = re.sub(r"\b(water year|calendar year)\s*(\d{4})-(\d{2})\b",
               lambda m: f"{m.group(1)} {_year(int(m.group(2)))}, {_two(int(m.group(3)))}", t)
    t = re.sub(r"(?<![\d,])\b(\d{4})-(\d{2})\b(?![\d-])", lambda m: f"{_year(int(m.group(1)))}, {_two(int(m.group(2)))}", t)
    codes = _station_codes()
    for key in sorted(codes, key=len, reverse=True):
        t = re.sub(rf"(?<![\w-]){re.escape(key)}-U(\d+)\b", lambda m, k=key: f"{codes[k]} unit {m.group(1)}", t)
        t = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", codes[key], t)
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = re.sub(r"\bW(\d{1,2})\b", r"week \1", t)
    for k, w in _UNIT_WORDS.items():
        t = re.sub(rf"(?<=\d)\s*{k}\b", " " + w, t)
    for a in _ACRONYMS:
        if a in _UNIT_WORDS:
            continue
        t = re.sub(rf"\b{a}\b", _spell(a.rstrip("s")) + ("s" if a.endswith("s") and a != "RES" else ""), t)
    t = re.sub(r"\bESCOMs\b", "Escoms", t)
    t = re.sub(r"\bESCOM\b", "Escom", t)
    t = re.sub(r"\s/\s", " or ", t.replace("&", " and "))
    return re.sub(r"\s{2,}", " ", t).strip()
