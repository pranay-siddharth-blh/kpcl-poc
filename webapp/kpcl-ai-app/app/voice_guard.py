"""
Voice guardrails (voice instance only). Everything here sits around the unchanged question pipeline:

  transcript checks   language (English / Kannada script only), length (seconds of audio, characters of text),
                      repeats (the same question twice within a few seconds from one session), the misheard-names
                      list (voice_lexicon.json, validated), and the "Did you mean ...?" confirmation for anything the
                      clean-up was unsure about (a loose name match, a repaired year, a single ambiguous year, a known
                      confusable pair such as Shiva / Sirwar)
  spoken answer rules read back the station and period first; ask again (with what was heard) instead of a flat
                      refusal; speak a model-written answer only if the second check passed; every spoken figure must be
                      a screen figure and carry its unit, or the voice says "the figures are on your screen" instead
  health              the speech services, checked at most every two minutes
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import struct
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import httpx

from app.config import REPO_ROOT, settings

_log = logging.getLogger(__name__)
LEXICON_PATH = Path(__file__).resolve().parent / "voice_lexicon.json"
MISHEARINGS_LOG = REPO_ROOT / "logs" / "voice_mishearings.jsonl"


# ================================================================================================ misheard names
@lru_cache(maxsize=1)
def lexicon() -> dict:
    """{'mishearings': {heard: means}, 'confusable': [...], 'rejected': [(entry, why)]} -- invalid entries are dropped
    (and listed), so a careless addition can never turn a real station name into another one."""
    from app.spoken import _ORDINARY, _name_vocab, _phon
    raw = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    from app.semantic_layer import router_index
    vocab = _name_vocab()                              # sound key -> name word the data knows
    ix = router_index()
    known = {w for aliases in list(ix.stations.values()) + list(ix.reservoirs.values()) + list(ix.suppliers.values())
             for a in aliases for w in re.findall(r"[a-z]+", a.lower())} - _ORDINARY
    out, rejected = {}, []
    for e in raw.get("mishearings", []):
        heard, means = str(e.get("heard", "")).strip().lower(), str(e.get("means", "")).strip()
        why = None
        if not re.fullmatch(r"[a-z]{3,30}", heard):
            why = "heard must be one word of letters"
        elif heard in known or _phon(heard) in vocab:
            why = "heard is (or sounds exactly like) a real station / reservoir / supplier name"
        elif heard in _ORDINARY:
            why = "heard is an ordinary question word"
        elif not means or means.split()[0].lower() not in known:
            why = "means is not a name the data knows"
        elif not e.get("reviewed_by"):
            why = "not reviewed"
        if why:
            rejected.append((e, why))
            _log.warning("voice_lexicon.json: ignoring %r -> %r: %s", heard, means, why)
            continue
        out[heard] = means
    confusable = [c for c in raw.get("confusable", []) if c.get("reviewed_by")]
    return {"mishearings": out, "confusable": confusable, "rejected": rejected}


def record_mishearing(entry: dict) -> None:
    """Text only (never audio), and only when VOICE_COLLECT_MISHEARINGS is on (with the officers' consent)."""
    if not settings.voice_collect_mishearings:
        return
    try:
        MISHEARINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MISHEARINGS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("could not record a mishearing: %s", exc)


# ================================================================================================ transcript checks
def wav_seconds(audio: bytes) -> float | None:
    """Duration of a PCM WAV from its header (None if it isn't one)."""
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    pos, rate, block = 12, None, None
    while pos + 8 <= len(audio):
        cid, size = audio[pos:pos + 4], struct.unpack("<I", audio[pos + 4:pos + 8])[0]
        if cid == b"fmt " and pos + 24 <= len(audio):
            _fmt, _ch, rate, _bps, block = struct.unpack("<HHIIH", audio[pos + 8:pos + 22])
        elif cid == b"data":
            n = min(size, len(audio) - pos - 8)
            return n / (rate * block) if rate and block else None
        pos += 8 + size + (size & 1)
    return None


_OTHER_SCRIPT = re.compile(r"[^\x00-ɏಀ-೿ -⁯₹\s\d.,'’\"?!:;()\-–/%&₹]")


def voiced_seconds(audio: bytes) -> float | None:
    """Seconds of a 16-bit PCM WAV clearly louder than its own background (20 ms frames over 2.5x the quietest tenth,
    and over an absolute floor). Office noise, a fan, a chime or a key press stays well under 0.35 s; a spoken question
    is over a second. None if the body is not a PCM WAV (then the speech model decides alone)."""
    try:
        import wave
        import numpy as np
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() != 2:
                return None
            rate, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
        x = np.frombuffer(raw[: len(raw) // (2 * ch) * 2 * ch], dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        n = max(1, int(rate * 0.02))
        frames = x[: len(x) // n * n].reshape(-1, n)
        if not len(frames):
            return 0.0
        rms = np.sqrt((frames ** 2).mean(axis=1))
        floor = float(np.percentile(rms, 10))
        return float((rms > max(2.5 * floor, 0.006)).sum() * n / rate)
    except Exception:  # noqa: BLE001
        return None


MIN_VOICED_SECONDS = 0.35


def too_short(cleaned: str, voiced: float | None, context_words: set[str]) -> bool:
    """A transcript that is not a question: one word ("PLF", "RTPS"), or two or three words that are all from the
    speech model's own hint paragraph out of under a second of speech -- what the model writes for noise."""
    words = re.findall(r"[a-z0-9']+", cleaned.lower())
    if len(words) <= 1:
        return True
    return len(words) <= 3 and voiced is not None and voiced < 1.0 and all(w in context_words for w in words)


def language_ok(text: str) -> bool:
    """English (Latin script) or Kannada script. A transcript mostly in another script (the model hearing noise as
    Chinese, or a question in another language) is dropped and the officer told so."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    other = len(_OTHER_SCRIPT.findall("".join(letters)))
    return other / len(letters) < 0.3


class RepeatGuard:
    """The same question from the same session within `window` seconds is a double-send or an echo: answer once."""

    def __init__(self, window: float = 4.0, max_sessions: int = 5000):
        self.window, self.max = window, max_sessions
        self.last: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def seen(self, session: str, text: str) -> bool:
        key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        now = time.monotonic()
        prev = self.last.get(session)
        self.last[session] = (key, now)
        self.last.move_to_end(session)
        while len(self.last) > self.max:
            self.last.popitem(last=False)
        return bool(key) and prev is not None and prev[0] == key and now - prev[1] < self.window


repeats = RepeatGuard()


# ================================================================================================ "Did you mean ...?"
@lru_cache(maxsize=1)
def _word_to_place() -> dict[str, str]:
    """Any alias word of a station / reservoir / supplier -> how to say that place in a question ("Sirwar mini hydel")."""
    from app.semantic_layer import load_semantic_layer, router_index
    ix, names = router_index(), {s["key"]: s["name"] for s in load_semantic_layer()["entities"]["stations"]}
    out: dict[str, str] = {}
    for key, aliases in ix.stations.items():
        for a in aliases:
            for w in re.findall(r"[a-z]+", a):
                out.setdefault(w, names.get(key, key))
    for res, aliases in ix.reservoirs.items():
        for a in aliases:
            out.setdefault(a, f"{res.title()} reservoir")
    return out


def place_label(word: str) -> str:
    return _word_to_place().get(word.lower(), word)


def _replace_first(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text, count=1, flags=re.I)


def confirmation(cleaned: str, notes: list[dict]) -> dict | None:
    """The one question to ask before answering, from the clean-up's notes: {'prompt', 'reason', 'options': [{'label',
    'question'}]}, the most likely option first. None when nothing was uncertain."""
    for n in notes:
        span = n.get("span", "")
        if not span or not re.search(rf"(?<![\w-]){re.escape(span)}(?![\w-])", cleaned, re.I):
            continue
        options, seen = [], set()
        for opt in n["options"]:
            label = place_label(opt) if n["kind"] == "name" else (opt if opt.upper().startswith("FY") else f"FY {opt}")
            if label.lower() in seen:
                continue
            seen.add(label.lower())
            q = cleaned if opt.lower() == span.lower() else re.sub(rf"(?<![\w-]){re.escape(span)}(?![\w-])", opt, cleaned,
                                                                   count=1, flags=re.I)
            options.append({"label": label, "question": q})
        if len(options) >= 2:
            return {"prompt": f"Did you mean {options[0]['label']}, or {options[1]['label']}?", "reason": n["kind"],
                    "options": options[:2]}
    return None


# ================================================================================================ spoken answer rules
_CATEGORY_WORDS = {"THERMAL-COAL": "thermal stations", "MAJOR HYDRO": "major hydro stations", "MINOR HYDRO": "minor hydro stations",
                   "GAS": "the gas station", "WTE": "waste to energy", "RES": "waste to energy", "SOLAR": "solar plants",
                   "WIND": "the wind farm"}
_TYPE_WORDS = {"HYDRO": "hydro stations", "THERMAL": "thermal stations", "SOLAR": "solar plants", "WIND": "the wind farm",
               "GAS": "the gas station", "WTE": "waste to energy"}


def readback(question: str, answer: str) -> str:
    """'For Sirwar Mini Hydel, FY 2024-25: ' -- the place and period the answer is about, said first so a misheard
    station or year is caught by ear. Empty when the question names neither."""
    from app.semantic_layer import extract_entities, load_semantic_layer
    e = extract_entities(question)
    names = {s["key"]: s["name"] for s in load_semantic_layer()["entities"]["stations"]}
    places: list[str] = []
    if e.get("units"):
        places += [u.replace("-U", " unit ") for u in e["units"][:2]]
    elif e.get("stations"):
        places += [names.get(s, s) for s in e["stations"][:2]]
    if e.get("reservoirs"):
        places += [f"{r.title()} reservoir" for r in e["reservoirs"][:2]]
    if e.get("suppliers"):
        places += [{"SCCL": "Singareni coal", "MCL": "Mahanadi coal", "WCL": "Western Coalfields coal",
                    "IMPORTED": "imported coal"}.get(s, s) for s in e["suppliers"][:2]]
    if not places and e.get("categories"):
        places += [_CATEGORY_WORDS.get(c, c.lower()) for c in e["categories"][:2]]
    if not places and e.get("plant_type"):
        places.append(_TYPE_WORDS.get(e["plant_type"], e["plant_type"].lower()))
    if not places and e.get("scope") == "company":
        places.append("all of KPCL")
    period = ""
    ps = [p for p in e.get("periods", []) if p.get("kind") != "out_of_range"]
    if ps:
        period = " and ".join(re.sub(r"\s*\(Jan-Dec\)", "", p["label"]) for p in ps[:2])
    elif e.get("as_of") or re.search(r"\b(now|today|currently|right now|latest)\b", question, re.I):
        period = "as on 30 April 2025"
    elif "No period was named" in (answer or ""):
        period = "FY 2024-25, the latest full year"
    from app.spoken import _dates
    period = _dates(period)
    if not places and not period:
        return ""
    if not places:
        return (period[0].upper() + period[1:] + ": ") if period.startswith("as on") else f"For {period}: "
    place = " and ".join(places)
    return f"For {place}, {period}: " if period else f"For {place}: "


ASK_AGAIN_ROUTES = ("unknown_name", "unknown_unit", "llm_unverified", "llm_sql_failed", "llm_declined", "llm_mismatch",
                    "refused:off_topic", "refused:empty")


def ask_again(question: str, route: str) -> str | None:
    """Instead of a flat refusal: say what was heard and ask once more."""
    if not route.startswith(ASK_AGAIN_ROUTES):
        return None
    heard = re.sub(r"\s+", " ", question).strip().rstrip("?.!")[:160]
    if route.startswith(("unknown_name", "unknown_unit")):
        return f"I heard: {heard}. I don't recognise that station or unit. Could you say the station name again?"
    if route.startswith("refused:"):
        return f"I heard: {heard}. That doesn't sound like a question about KPCL's generation data. Could you say it again?"
    return f"I heard: {heard}. I couldn't confirm an answer to that. Could you say it again, with the station and the period?"


MODEL_UNCHECKED = "The answer is on your screen. I couldn't double-check it, so I won't read it out; please check it there."
FIGURES_ON_SCREEN = "The figures are on your screen."

_SPOKEN_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(crore|lakh)?")
_DATEY = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b|\b(?:FY ?)?(?:19|20)\d{2}(?:-\d{2})?\b|\b\d{1,2} (?:January|February|March|April|May|June|July|"
                    r"August|September|October|November|December)\b|\b(?:January|February|March|April|May|June|July|August|"
                    r"September|October|November|December) \d{1,2}\b|\b\d{1,2}-[A-Za-z]{3}-\d{4}\b|\bQ[1-4]\b|"
                    r"\bunit \d+\b|\bU\d+\b|\b(?:YAL|ITN|YAP)-SOLAR-\d\b|\bSH-SOLAR-\d+\b|\bweek \d+\b|\bW\d+\b")
_UNIT_AFTER = re.compile(
    r"^\s*(?:percent|per cent|points|million units|million|crore|lakh|thousand|tonnes|tons|hours|days|MW|GW|"
    r"gigawatts?|kilowatts?|megawatts?|terawatts?|feet|metres|meters|kilocalories|kilograms|millilitres|million cubic feet|"
    r"million standard cubic metres|MMSCM|hertz|"
    r"cubic|kilolitres|mm|millimetres|units|unit|stations|station|trips|times|outages|reasons|months|weeks|rakes|"
    r"failures|plants|reservoirs|of \d+|out of|suppliers|years|hydel|TMC|MCFT|GWh|kWh|MWh|MU|%|Hz|hertz)\b", re.I)


def _values(text: str, scaled: bool) -> list[float]:
    out = []
    for w, f, scale in _SPOKEN_NUM.findall(_DATEY.sub(" ", text)):
        v = float(w.replace(",", "") + ("." + f if f else ""))
        out.append(v * ({"crore": 1e7, "lakh": 1e5}.get(scale, 1) if scaled else 1))
    return out


def figures_match_screen(spoken: str, answer: str) -> bool:
    """Every figure the voice says is a figure on the screen (rounded as said; lakh / crore compared both ways)."""
    screen = _values(answer, scaled=False) + _values(answer, scaled=True)
    return all(any(abs(v - x) <= max(0.0051 * abs(x), 0.5) for x in screen) for v in _values(spoken, scaled=True))


# A measure the data always keeps in one unit: a bare figure after it is said with that unit.
_FIXED_UNIT = ((re.compile(r"\bheat rates?\b", re.I), "kilocalories per kilowatt-hour"),
               (re.compile(r"\bfrequency\b", re.I), "hertz"))
# What a whole number can count; said earlier in the sentence, it names the figure ("RTPS had 35 ... failures", "Trips: 11").
_COUNTED = re.compile(r"\b(?:trips?|tripped|count|rakes?|outages?|failures?|reasons?|stations?|(?<!million )(?<!lakh )"
                      r"(?<!crore )units?|times|plants?|suppliers?|reservoirs?|shutdowns?|events?)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_UNIT_IN_LABEL = re.compile(r"\b(?:lakh tons|lakh tonnes|tonnes|tons|million units|MU|percent|hours|days|megawatts|MW|"
                            r"hertz|feet|metres|crore units|lakh units|kilocalories per kilowatt-hour)\)?\s*:\s*$", re.I)


def _masked(text: str) -> str:
    """Dates, years, unit names blanked out (same length, so positions still line up)."""
    return _DATEY.sub(lambda m: " " * len(m.group()), text)


def _unit_follows(masked: str, m: re.Match) -> bool:
    tail = masked[m.end():m.end() + 60]
    if m.group(3) or re.match(r"^-(?:day|hour|month|year)s?\b", tail, re.I):
        return True
    tail = re.sub(r"^\s*(?:about|nearly|around|over|under|more than|less than|of the|of|the)\s+", " ", tail)
    return bool(_UNIT_AFTER.match(tail) or re.match(r"^\s*(?:[\w,.-]+\s+){0,3}(?:units|stations|trips|times|outages|"
                                                     r"reasons|months|weeks|rakes|failures|plants|reservoirs|suppliers|"
                                                     r"years|days|hours)\b", tail, re.I))


def add_units(spoken: str) -> str:
    """'heat rate, 2,255' -> 'heat rate, 2,255 kilocalories per kilowatt-hour'; 'Min frequency: 49.82' -> '... 49.82 hertz'."""
    out = []
    for sent in _SENTENCE.split(spoken):
        for pat, unit in _FIXED_UNIT:
            hit = pat.search(sent)
            if not hit:
                continue
            masked = _masked(sent)
            for m in reversed(list(_SPOKEN_NUM.finditer(masked, hit.end()))):
                if not _unit_follows(masked, m):
                    cut = m.start(1) + len(m.group(0).rstrip())
                    sent = sent[:cut] + " " + unit + sent[cut:]
        out.append(sent)
    return " ".join(out)


def figures_have_units(spoken: str) -> bool:
    """No figure the listener can't place: each has its unit after it, or is a whole-number count of something the same
    sentence has already named ('trips ... 33', 'Outage count: 31'). A bare decimal never passes."""
    for sent in _SENTENCE.split(spoken):
        masked = _masked(sent)
        for m in _SPOKEN_NUM.finditer(masked):
            if _unit_follows(masked, m):
                continue
            if not m.group(2) and _COUNTED.search(sent[:m.start()]):
                continue
            if _UNIT_IN_LABEL.search(sent[:m.start()]):
                continue
            return False
    return True


# ================================================================================================ health
_health: dict = {"at": 0.0, "value": None}
_health_lock = asyncio.Lock()


async def speech_health(force: bool = False) -> dict:
    """{'speech_in': {'ok', 'detail'}, 'speech_out': {...}, 'checked': iso}. Each service's model list is fetched with
    its own key (no audio, no GPU work); cached for two minutes."""
    async with _health_lock:
        if not force and _health["value"] and time.monotonic() - _health["at"] < 120:
            return _health["value"]

        async def probe(enabled: bool, base: str, key: str, model: str) -> dict:
            if not enabled:
                return {"ok": False, "detail": "not configured"}
            try:
                async with httpx.AsyncClient(verify=settings.ai_model_ca_bundle_path or False, timeout=6) as c:
                    r = await c.get(f"{base.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
                if r.status_code != 200:
                    return {"ok": False, "detail": f"gateway answered {r.status_code}"}
                ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
                return {"ok": True, "detail": "ready" if model in ids or not ids else f"{model} not listed; gateway reachable"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": f"unreachable ({type(exc).__name__})"}

        async def probe_tts() -> dict:
            """The voice key may not list models, so ask for one short word and stop at the first audio."""
            if not settings.tts_configured:
                return {"ok": False, "detail": "not configured"}
            from app import voice
            stream = voice.stream_speech("OK.")
            try:
                await asyncio.wait_for(stream.__anext__(), timeout=10)
                return {"ok": True, "detail": "ready"}
            except voice.VoiceBusy:
                return {"ok": True, "detail": "busy"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": f"unavailable ({type(exc).__name__})"}
            finally:
                try:
                    await stream.aclose()
                except Exception:  # noqa: BLE001
                    pass

        speech_in, speech_out = await asyncio.gather(
            probe(settings.asr_configured, settings.asr_base_url, settings.asr_api_key, settings.asr_model), probe_tts())
        value = {"speech_in": speech_in, "speech_out": speech_out, "checked": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _health.update(at=time.monotonic(), value=value)
        return value
