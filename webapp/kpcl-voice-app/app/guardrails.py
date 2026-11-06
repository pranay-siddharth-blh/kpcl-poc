"""
Guardrails around the chatbot (port of the WB POC's guardrails, with KPCL vocabulary):

  1. screen_question  -- before any routing: empty / too long, pasted SQL or code, prompt injection, questions
     about the system itself, and questions with nothing to do with KPCL generation data.
  2. REFUSALS         -- fixed, reviewed wording for every refusal.
  3. scrub            -- drops any sentence of model-written text that names an internal object (table or column
     names, SQLite, the database file, the model) and rewrites jargon into plain words.
  4. safe_refusal     -- the model's own "can't answer" text is shown only if every sentence is about the data.
  5. subject_mismatch -- a "how many <things>" question answered by SQL that never touches <things>.
The database-level guard (read-only file, authorizer) is in app.db; SQL checks are in app.sql_safety.
"""
from __future__ import annotations

import re

from app.semantic_layer import load_semantic_layer

MAX_QUESTION_CHARS = 500

REFUSALS = {
    "empty": "Please type a question about KPCL's generation data.",
    "too_long": f"That question is too long. Please keep it under {MAX_QUESTION_CHARS} characters and ask one thing at a time.",
    "system": ("I can only answer questions about KPCL's generation data, not about how this assistant is built. Try asking "
               "about generation, PLF, outages, coal, reservoirs or targets."),
    "injection": "I can't follow instructions like that. I can answer questions about KPCL's generation data.",
    "code": ("I can't run queries or code typed into the chat. Please ask in plain words, for example: "
             "\"What was RTPS's PLF in March 2025?\""),
    "off_topic": ("I can only help with KPCL's power generation data: stations and units, generation, PLF and PAF, "
                  "auxiliary consumption, outages and trips, back-down, coal, reservoirs, the state grid and targets. "
                  "For example: \"Which stations missed their CEA target in March 2025?\""),
    "not_in_data": ("I can't answer that reliably from the data available. Try rephrasing, or name the station, unit, "
                    "measure and period you mean."),
    "mismatch": "I couldn't find a reliable answer to that exact question. Please rephrase it, naming the station or period you mean.",
    "llm_unavailable": ("I can answer that kind of question only with the language model, which isn't reachable right now "
                        "(it runs on the internal gateway, so the VPN must be connected). Single figures such as "
                        "\"RTPS PLF in March 2025\", definitions, and the standard reports still work."),
    "error": "Sorry, I couldn't answer that right now. Please try again in a moment.",
    "read_only": ("I can't change, add or delete any data: this assistant only reads KPCL's generation records. "
                  "Corrections to the source data have to go through the Generation Management System itself."),
    "harmful": ("I can't help with anything that could damage equipment, endanger people or disrupt the grid. I can "
                "answer questions about KPCL's generation data."),
    "personal_data": ("I can't share details about individual people, such as names, phone numbers, addresses or salaries. "
                      "This data holds no personal records; it covers stations, units and their performance."),
    "unverified": ("I couldn't confirm a reliable answer to that: a second check of the figures did not agree with the question. Please rephrase it, naming the station or unit, the measure and the period you mean."),
    "rate_limited": "Too many questions in a short time. Please wait a moment and try again.",
    "busy": "The assistant is busy answering other questions. Please try again in a moment.",
}

# ---------------------------------------------------------------------------------------------- 1. screening
_SQL_OR_CODE_RE = re.compile(
    r"(\bselect\b[\s\S]{0,200}\bfrom\b|\b(insert\s+into|delete\s+from|drop\s+(table|view|database)|update\s+\w+\s+set|"
    r"alter\s+table|create\s+(table|view|index)|truncate\s+table|attach\s+database|pragma\s+\w+|union\s+(all\s+)?select)\b|"
    r";\s*--|/\*|\*/|<\s*script|\bimport\s+os\b|\bexec\s*\(|\beval\s*\(|\bos\.system\b|\bsubprocess\b)", re.IGNORECASE)
_INJECTION_RE = re.compile(
    r"(ignore|disregard|forget|override|bypass)\b[^.?!]{0,40}\b(previous|prior|above|earlier|all|your|the)\b[^.?!]{0,30}"
    r"\b(instructions?|rules?|prompts?|guidelines?|context)|\b(system|developer|hidden|initial)\s+(prompt|message|instructions?)\b|"
    r"\b(reveal|show|print|repeat|output|display|tell me)\b[^.?!]{0,30}\b(your|the)\s+(prompt|instructions?|rules|configuration|config)\b|"
    r"\byou are now\b|\bact as\b|\bpretend (to be|you)\b|\bjailbreak\b|\bdeveloper mode\b|\bdo anything now\b|\bno restrictions\b",
    re.IGNORECASE)
_SYSTEM_RE = re.compile(
    r"\b(databases?|db|sqlite|sql|schemas?|tables?|columns?|queries|query language|backend|back-end|server|source code|"
    r"codebase|api keys?|endpoint|prompt|llm|language model|gpt|qwen|openai|claude|vllm|which model|what model|file ?paths?|"
    r"filenames?|\.db|python|fastapi|etl|pipeline|semantic layer|mart|staging|how (are|were) you (built|made|trained)|"
    r"your (code|instructions|training|architecture))\b", re.IGNORECASE)
_SYSTEM_EXCEPTIONS_RE = re.compile(
    r"\b(as a table|in a table|table of|tabular|in (one|a single|the same|a) table|(one|single|summary|comparison) table|"
    r"table (showing|with|for|by|comparing))\b", re.IGNORECASE)
_DOMAIN_WORDS = (
    r"generat\w*|power|energy|electricity|plf|paf|load factor|availability|capacity|mw|mu|kwh|stations?|plants?|units?|"
    r"thermal|hydro\w*|hydel|solar|wind|gas|waste|coal|oil|hfo|ldo|gcv|heat rate|specific|aux\w*|net export|export|"
    r"outages?|trips?|tripp\w*|forced|planned|shut\s?down|rsd|btf|btl|boiler|tube|back-?\s?down|backdown|nld|deemed|"
    r"loss|losses|targets?|cea|kerc|achiev\w*|reservoirs?|dams?|water|inflow|rainfall|storage|level|frl|mddl|grid|"
    r"state|consumption|demand|import|frequency|peak|share|kpcl|rpcl|kpcgpcl|mpr|jmr|meter|bill\w*|escom\w*|tariff|"
    r"defect\w*|fy|financial year|water year|month\w*|quarter|season|monsoon|summer|winter|daily|yearly|annual|trend|"
    r"running|hours|commission\w*|cumulative|data|figures?|report|dashboard|rakes?|supplier|stock|norm|status|"
    # a month with its year ("March 2024", "mar-24") or an FY written bare ("2023-24"): "what was in March 2024?"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s,'-]*(?:19|20)?\d\d|(?:19|20)\d\d\s*-\s*\d\d"
)


def _domain_re() -> re.Pattern:
    layer = load_semantic_layer()
    ents = layer["entities"]
    names = {s["key"].lower() for s in ents["stations"]}
    for aliases in ents["station_aliases"].values():
        names.update(a.lower() for a in aliases)
    for aliases in ents["reservoir_aliases"].values():
        names.update(a.lower() for a in aliases)
    extra = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"\b(" + _DOMAIN_WORDS + "|" + extra + r")\b", re.IGNORECASE)


_DOMAIN_CACHE: list[re.Pattern] = []
_GENERAL_KNOWLEDGE_RE = re.compile(
    r"\b(capital|chief minister|governor|prime minister|president|minister|elections?|history of|founded|independence|"
    r"weather forecast|temperature today|tourist|tourism|places to visit|cricket|football|films?|movies?|actors?|songs?|"
    r"recipes?|jokes?|poems?|stories|stock price|share price|news)\b", re.IGNORECASE)
_DATA_TERMS_RE = re.compile(r"\b(generat\w*|plf|paf|outages?|coal|reservoirs?|dams?|stations?|units?|mu|targets?)\b", re.I)


# Requests to change data. The database is opened read-only anyway; this gives a clear answer instead of a figure.
_WRITE_RE = re.compile(
    r"^\s*(?:please\s+|kindly\s+|can you\s+|could you\s+|i want you to\s+|go ahead and\s+|now\s+)?"
    r"(update(?!\s+(me|us)\b)|delete|remove|modify|edit|overwrite|insert|alter|erase|wipe|reset|upload|replace|"
    r"change(?!\s+(in|of|over|from|between)\b)|set(?!\s+of\b)|add(?!\s+up\b)|correct(?!ly)|fix)\b|"
    r"\b(update|overwrite|modify)\b[^.?!]{0,60}\b(to|=|as)\s+-?[\d₹]", re.IGNORECASE)
# Anything that could hurt people, damage plant or disrupt the grid.
_HARMFUL_RE = re.compile(
    r"\b(sabotag\w*|bombs?|explosives?|terror\w*|weapons?|poison\w*|arson)\b|"
    r"\bhow\s+(do|can|could|would|should)\s+(i|we|someone|one)\b[^.?!]{0,50}\b(damage|destroy|attack|hack|disable|bypass|"
    r"blow up|explode|overload|crash)\b|"
    r"\bhow to\b[^.?!]{0,50}\b(damage|destroy|attack|hack|disable|bypass|blow up|explode|overload|crash)\b|"
    r"\b(disable|bypass|defeat|override)\s+(the\s+)?(safety|protection|interlock|relay|alarm|scada)\w*", re.IGNORECASE)
# Individual people: this data has none, and the assistant must never guess.
_PERSONAL_DATA_RE = re.compile(
    r"\b(phone|mobile|contact|cell|telephone)\s*(numbers?|nos?|details)\b|\be-?mail (ids?|address\w*)\b|"
    r"\b(names?|address\w*|salar\w+|pay|details|personal (data|details|information))\s+of\s+(the\s+|all\s+)?"
    r"(?:[\w-]+\s+){0,4}?(engineers?|staff|employees?|officers?|operators?|workers?|workmen|people|persons?|managers?|"
    r"supervisors?|contractors?|individuals?)\b|"
    r"\b(aadhaar|aadhar|pan (card|number)|bank account|home address|date of birth)\b", re.IGNORECASE)


def screen_question(question: str) -> str | None:
    """Return a REFUSALS key if the question must not go further, else None."""
    q = (question or "").strip()
    if not q or not re.search(r"[A-Za-zಀ-೿]", q):
        return "empty"
    if len(q) > MAX_QUESTION_CHARS:
        return "too_long"
    if _SQL_OR_CODE_RE.search(q):
        return "code"
    if _INJECTION_RE.search(q):
        return "injection"
    if _HARMFUL_RE.search(q):
        return "harmful"
    if _WRITE_RE.search(q):
        return "read_only"
    if _PERSONAL_DATA_RE.search(q):
        return "personal_data"
    if _SYSTEM_RE.search(q) and not _SYSTEM_EXCEPTIONS_RE.search(q):
        return "system"
    if re.search(r"[ಀ-೿]", q):
        return None  # Kannada text: let it through; the prompt handles scope
    if not _DOMAIN_CACHE:
        _DOMAIN_CACHE.append(_domain_re())
    if not _DOMAIN_CACHE[0].search(q):
        return "off_topic"
    if _GENERAL_KNOWLEDGE_RE.search(q) and not _DATA_TERMS_RE.search(q):
        return "off_topic"
    return None


# ---------------------------------------------------------------------------------------------- 3. scrubbing
_JARGON = {
    r"\bgross_gen_mu\b": "gross generation", r"\bnet_export_mu\b": "net export", r"\bplf_pct\b": "PLF",
    r"\bpaf_pct\b": "PAF", r"\baux_pct\b": "auxiliary consumption %", r"\bstation_key\b": "station",
    r"\bunit_key\b": "unit", r"\bplant_category_label\b": "plant category", r"\bcea_target_mu\b": "CEA target",
    r"\bfy_quarter\b": "quarter", r"\bmonth_label\b": "month", r"\bdate_key\b": "date",
}
_INTERNAL_RE = re.compile(
    r"\b(core|mart|stg|raw|semantic|ledger|src|ref|sqlite)_[a-z0-9_]+\b|\bkpcl_gms\b|\.db\b|\bsqlite\b|\bsql\b|"
    r"\bquer(y|ies)\b|\bdatabases?\b|\btables?\b|\bcolumns?\b|\bschema\b|\bjoin(ed|s)?\b|\bprompt\b|\bllm\b|"
    r"\blanguage model\b|\bplaceholder\b|\b[a-z]+_[a-z0-9]+(_[a-z0-9]+)*\b|\b\w+\.(py|json|sql|xlsx?|csv)\b|"
    r"\bstar schema\b|\bmarts?\b|\bstaging\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(₹*])")


def _dejargon(text: str) -> str:
    for pattern, plain in _JARGON.items():
        text = re.sub(pattern, plain, text, flags=re.IGNORECASE)
    return text


def scrub(text: str) -> str:
    """Rewrite known jargon, then drop every sentence that still names an internal object."""
    if not text:
        return ""
    text = re.sub(r"\s*\([^()]*\)", lambda m: "" if _INTERNAL_RE.search(m.group(0)) else m.group(0), _dejargon(text))
    paras = []
    for para in text.split("\n\n"):
        lines = []
        for line in para.split("\n"):
            sentences = [s for s in _SENTENCE_SPLIT_RE.split(line) if s.strip()]
            clean = [s for s in sentences if not _INTERNAL_RE.search(s)]
            if clean:
                lines.append(" ".join(clean))
        if lines:
            paras.append("\n".join(lines))
    return "\n\n".join(paras).strip()


_REFUSAL_SENTENCE_RE = re.compile(
    r"\b(data|records?|information|details?|available|contain\w*|cover\w*|includ\w*|only|not|no|cannot|can't|couldn't|"
    r"unable|limited|missing|stations?|units?|generation|please|try|ask|rephras\w*|period|months?|years?|fy|loaded|"
    r"measured|recorded|reports?)\b", re.IGNORECASE)


def safe_refusal(llm_message: str) -> str:
    """The model's reason for not answering, if short and clean and entirely about the data; else a fixed text."""
    raw = _dejargon(llm_message or "").strip()
    sentences = [x for x in _SENTENCE_SPLIT_RE.split(raw) if x.strip()]
    if not sentences or any(not _REFUSAL_SENTENCE_RE.search(x) for x in sentences):
        return REFUSALS["not_in_data"]
    if len(raw) <= 400 and not _INTERNAL_RE.search(raw):
        return raw
    return REFUSALS["not_in_data"]


# ---------------------------------------------------------------------------------------------- 4b. no provenance
# General-knowledge answers read like any other answer: no source lists, web addresses, site names or notes about
# where the text came from ("knowledge base", "general information", "YAML"). Sources stay in the YAML file only.
_PROVENANCE_RE = re.compile(
    r"^\s*sources?\s*:|https?://|\bwww\.|\b[\w-]+\.(com|org|net|in|gov|nic|edu|io|info)\b|\bwikipedia\b|"
    r"\bknowledge[- ]base\b|\byaml\b|\bgeneral (information|knowledge)\b|\bnot from (kpcl'?s )?(the )?(generation )?data\b",
    re.IGNORECASE)


def strip_provenance(text: str) -> str:
    """Drop every sentence that cites a source or says where the answer came from; keep the rest intact."""
    if not text:
        return ""
    paras = []
    for para in text.split("\n\n"):
        lines = []
        for line in para.split("\n"):
            if _PROVENANCE_RE.search(line):
                kept = [s for s in _SENTENCE_SPLIT_RE.split(line) if s.strip() and not _PROVENANCE_RE.search(s)]
                line = " ".join(kept)
            if line.strip():
                lines.append(line)
        if lines:
            paras.append("\n".join(lines))
    return "\n\n".join(paras).strip()


# ---------------------------------------------------------------------------------------------- 5. subject check
_COUNT_SUBJECT_RE = re.compile(
    r"\b(how many|number of|count of|total number of|no\.? of)\s+(distinct\s+|unique\s+|different\s+)?"
    r"(stations|plants|units|outages|trips|boiler tube failures|failures|days|reservoirs|dams|rakes|months)\b", re.I)
_SUBJECT_SQL = {"stations": r"station", "plants": r"station", "units": r"unit", "outages": r"outage", "trips": r"trip|outage",
                "boiler tube failures": r"btf", "failures": r"btf|outage", "days": r"date_key|days|is_below_norm|count",
                "reservoirs": r"reservoir", "dams": r"reservoir", "rakes": r"rake", "months": r"month"}


_SUBSIDIARY_RE = re.compile(r"\b(rpcl|kpcgpcl|raichur power corporation|gas power corporation|subsidiar\w*|"
                            r"company[- ]wise|by company|each company|per company|company code)\b", re.IGNORECASE)


def company_filter_unrequested(question: str, sql: str) -> bool:
    """KPCL totals include the RPCL (YTPS) and KPCGPCL (YCCPP) stations; a company filter the question did not ask
    for silently drops them (the model once reported 99 units and 7.15 GW instead of 103 and 9.12 GW)."""
    return bool(re.search(r"\bcompany_(code|name)\b", sql or "", re.IGNORECASE)) and not _SUBSIDIARY_RE.search(question or "")


_COVERAGE_CLAIM_RE = re.compile(
    r"\b(outside|out of range|beyond|not (include|contain|cover|available|present)\w*|only (contains?|covers?|has|have|"
    r"includes?|holds?|from)|starting from|starts from|no (historical |such )?(data|records?)|does(n't| not) (include|"
    r"contain|cover|have|go back))\b", re.IGNORECASE)


def false_coverage_claim(message: str, entities: dict) -> bool:
    """The model declined because a period is 'outside the data' although every period asked about is inside it."""
    if not message or not _COVERAGE_CLAIM_RE.search(message):
        return False
    if not re.search(r"\b(20\d\d|fy|year|period|range|date|month|data)\b", message, re.IGNORECASE):
        return False
    return not any(p.get("kind") == "out_of_range" for p in entities.get("periods", []))


# Physical bounds a model-written result must respect before any figure is shown.
_BOUNDED_PCT_RE = re.compile(r"(plf|paf|aux|share|capacity_pct|storage|availability|load_factor|cuf)", re.IGNORECASE)
_CHANGE_RE = re.compile(r"(diff|change|growth|delta|yoy|vs|gap|shortfall|variance|minus|net_export|surplus|deficit)", re.IGNORECASE)
_NONNEG_RE = re.compile(r"(gross_gen|generation|_mu$|_hrs$|hours|trips|count|coal|stock|consumed|receipt|rakes|capacity_mw)",
                        re.IGNORECASE)


def implausible_result(columns: list[str], rows: list[tuple]) -> str | None:
    """A reason if a model-written result breaks a physical bound (PLF above 100 %, negative generation ...), else None.
    The pipeline then sends the reason back for one repair round instead of showing an impossible figure."""
    for i, c in enumerate(columns):
        if _CHANGE_RE.search(c):
            continue
        vals = [r[i] for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)]
        if not vals:
            continue
        if _BOUNDED_PCT_RE.search(c) and ("pct" in c.lower() or "%" in c) and (max(vals) > 100.5 or min(vals) < 0):
            return f"column {c} has {max(vals) if max(vals) > 100.5 else min(vals)}, but a percentage of this kind must be between 0 and 100"
        if _NONNEG_RE.search(c) and min(vals) < 0:
            return f"column {c} has a negative value ({min(vals)}), which is impossible for this measure"
    return None


def subject_mismatch(question: str, sql: str) -> bool:
    m = _COUNT_SUBJECT_RE.search(question or "")
    if not m:
        return False
    return not re.search(_SUBJECT_SQL[m.group(3).lower()], sql or "", re.IGNORECASE)
