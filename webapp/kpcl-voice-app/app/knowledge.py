"""
General-knowledge answers from kpcl_semantic_layer/knowledge_base.yaml: what KPCL is, who owns it, why it exists,
its history and subsidiaries, what a station is, the laws, regulators and rules it works under, and how it is
organised (Board, committees, directors, hierarchy, cadres, offices), its own policies (CSR, whistle blower, service
rules, recruitment, RTI, procurement) and what it publishes.

Two entry points, both deterministic (no LLM):
  * primary(question)  - a general question with no data intent ("What is KPCL?", "When was KPCL founded?",
                         "What is KERC?", "Tell me about RTPS"). A question that names a period, a measure or a unit,
                         or asks for counts, rankings or trends, is left to the warehouse.
  * fallback(question) - used when the warehouse path cannot answer: any clear match, unless the question asks for data.
Answers read like any other answer: no sources, site names or "from the knowledge base" notes are shown (the sources
stay in the YAML for maintainers; guardrails.strip_provenance enforces this).
Figures about generation, PLF, outages, coal or reservoirs never come from here.
"""
from __future__ import annotations

import re
from functools import lru_cache
import yaml

from app.config import SEMANTIC_DIR
from app.guardrails import strip_provenance
from app.semantic_layer import extract_entities

KNOWLEDGE_PATH = SEMANTIC_DIR / "knowledge_base.yaml"

# Data intent: these questions belong to the warehouse, not the knowledge base.
_DATA_INTENT_RE = re.compile(
    r"\b(how many|number of|count|total|how much|percentage|percent|share of|proportion|ratio|average|median|"
    r"which (station|unit|plant|month|year|reservoir)s?|rank\w*|highest|lowest|most|least|top|bottom|best|worst|"
    r"trend|forecast|compare|versus|vs\.?|largest|smallest|biggest|maximum|minimum|figures?|statistics|numbers?|"
    r"generat(ed|ion)|plf|paf|outages?|trips?|coal|stock|level|inflow|target|achievement|loss(es)?|mu|"
    r"list|show|name (all|the|every)|give me|stations|units|plants|in (gw|mw|kw|gigawatts?|megawatts?))\b", re.I)
# General-question phrasing (needed only for longer questions).
_GENERAL_RE = re.compile(
    r"\b(what is|what's|whats|what are|what does|what do|who is|who are|who owns|who runs|tell me about|explain|describe|"
    r"about|when was|when did|founded|incorporated|established|formed|set up|history|started|purpose|why|role|"
    r"objective|mission|how does|how do|meaning|mean|stand for|define|overview|introduction|where is|located|location|"
    r"headquarters|head office|address|owner|owned|ownership|subsidiar\w*|laws?|rules?|regulat\w*|act|"
    r"reports? to|report to|heads?|in charge|responsib\w*|structure|hierarchy|organi[sz](ed|ation)|procedure|process|"
    r"how (to|can|do i|does one|is|are)|apply|policy|policies|manual|committees?|board|powers?|duties|roles?)\b", re.I)
_STATION_PROFILE_RE = re.compile(
    r"^\s*(what is|what's|whats|tell me about|about|describe|explain|introduce|overview of|info(rmation)? (on|about))\b|"
    r"\b(where is|located|location|which river|what river|which district|commissioned|when was|built|history|"
    r"who owns|owned by|owner)\b", re.I)

# Which parts of the KPCL entry a question asks for.
_KPCL_FACETS = {
    "office_holders": re.compile(r"\b(md|managing director|chairman|chairperson|chair|ceo|cmd|directors?|board|"
                                 r"who (heads|leads|runs|manages)|head of|in charge|officials?|officers?|minister)\b", re.I),
    "founded": re.compile(r"\b(when|founded|incorporated|established|formed|set up|started|began|begin|origin|year)\b", re.I),
    "mission": re.compile(r"\b(mission|vision|motto|values|objectives|aims?|goals?)\b", re.I),
    "why_it_exists": re.compile(r"\b(why|purpose|reason|objective|role|need)\b", re.I),
    "regulated_by": re.compile(r"\b(regulat\w*|rules?|laws?|acts?|legislation|complian\w*|comply|governed|oversee\w*|"
                               r"oversight|answer(s|able)? to|audit\w*|norms)\b", re.I),
    "what_it_does": re.compile(r"\b(what does|what do|do\b|does\b|function|job|work|business|responsib\w*)", re.I),
    "ownership": re.compile(r"\b(who owns|owns|owner|owned|ownership|parent|subsidiar\w*|joint venture|jv|government)\b", re.I),
    "head_office": re.compile(r"\b(head ?office|headquarter\w*|hq|registered office|address|located|location|where is)\b", re.I),
    "legal_status": re.compile(r"\b(cin|legal|status|psu|public sector|state government company|companies act|type of company)\b", re.I),
    "capacity": re.compile(r"\b(capacity|how big|size|megawatts?|mw)\b", re.I),
    "generation_mix": re.compile(r"\b(types?|kinds?|sources?|mix|modes?|hydro and thermal|what sort)\b", re.I),
    "history": re.compile(r"\b(history|milestones?|timeline|evolution|journey)\b", re.I),
    "in_this_data": re.compile(r"\b(in (this|the) (data|system)|what data|data (do )?you have|dataset|coverage)\b", re.I),
}
_FACET_LABELS = {"office_holders": "Management", "founded": "Founded", "mission": "Mission", "why_it_exists": "Why KPCL exists",
                 "regulated_by": "Laws and regulators",
                 "what_it_does": "What KPCL does",
                 "ownership": "Ownership", "head_office": "Head office", "legal_status": "Legal status",
                 "capacity": "Installed capacity", "generation_mix": "How KPCL generates", "history": "Milestones",
                 "in_this_data": "In this system"}


@lru_cache(maxsize=1)
def _kb() -> dict:
    return yaml.safe_load(KNOWLEDGE_PATH.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "


def _has_alias(q: str, aliases: list[str]) -> bool:
    return any(_norm(a) in q for a in aliases)


def _card(title: str, body: str, extra: str = "") -> str:
    """An answer written like any other: no sources, no "from the knowledge base" note (sources stay in the YAML for
    maintainers only). guardrails.strip_provenance is applied on the way out as well."""
    return strip_provenance(f"**{title}**\n\n{body.strip()}" + (f"\n\n{extra}" if extra else ""))


# ---------------------------------------------------------------------------------------------- answers
def _kpcl_answer(question: str) -> str:
    k = _kb()["kpcl"]
    facets = [f for f, rx in _KPCL_FACETS.items() if rx.search(question)]
    if "what_it_does" in facets and len(facets) > 1:   # "why does KPCL exist" is about purpose, not activities
        facets.remove("what_it_does")
    if "office_holders" in facets:                     # "who is the MD" is about people only
        facets = ["office_holders"]
    if "mission" in facets and "why_it_exists" in facets:   # "mission and objectives": the stated mission
        facets.remove("why_it_exists")
    parts = []
    for f in facets:
        if f == "history":
            parts.append("\n".join(f"•  {h}" for h in k["history"]))
        elif f == "founded":
            parts.append(k["founded"].strip())
        else:
            parts.append(k[f].strip())
    if not parts:
        parts = [k["summary"].strip(), k["generation_mix"].strip()]
        title = k["name"]
    else:
        title = f"{k['name']}: " + ", ".join(_FACET_LABELS[f].lower() if i else _FACET_LABELS[f] for i, f in enumerate(facets))
    extra = "Try asking: \"How did KPCL do in FY 2024-25?\" or \"What was in March 2024?\""
    return _card(title, "\n\n".join(parts), extra)


def _entry_answer(e: dict) -> str:
    return _card(e["name"], e["summary"], e.get("in_this_data", "").strip())


def _station_answer(s: dict) -> str:
    extra = (f"For its figures, ask for example: \"{s['key']} PLF in FY 2024-25\", \"{s['key']} generation in March 2025\" "
             f"or \"How did {s['key']} do in FY 2024-25?\"")
    return _card(f"{s['name']} ({s['key']}) · {s['type']}, {s['capacity_mw']:,g} MW, {s['units']} unit{'s' if s['units'] != 1 else ''}",
                 s["summary"], extra)


def _glossary_answer(question: str) -> str | None:
    q = _norm(question)
    for term, meaning in _kb()["glossary"].items():
        if _norm(term) in q:
            return strip_provenance(f"**{term}**: {meaning}")
    return None


# Entry groups, each a list of {name, aliases, summary}. The organisation, policy and public-information groups are
# about KPCL itself; the others are the laws, projects and companies around it.
ENTRY_GROUPS = ("organisation", "policies", "public_information", "regulation", "projects", "companies")
ORGANISATION_GROUPS = ("organisation", "policies", "public_information")


def _best_entry(q: str, groups: tuple[str, ...] = ENTRY_GROUPS) -> dict | None:
    """The entry whose alias matches the most specific (longest) part of the question: "CSR committee" wins over
    "CSR", "Varahi pumped storage" over "pumped storage"."""
    kb, best, best_len = _kb(), None, 0
    for group in groups:
        for e in kb.get(group, []):
            for a in e["aliases"]:
                n = _norm(a)
                if n in q and len(n) > best_len:
                    best, best_len = e, len(n)
    return best


def is_about_organisation(question: str) -> bool:
    """The question names one of KPCL's own bodies, rules, policies or publications (board, CSR, RTI, cadres ...)."""
    return _best_entry(_norm(question), ORGANISATION_GROUPS) is not None


def _match(question: str, entities: dict, profiles: bool) -> str | None:
    q = _norm(question)
    kb = _kb()
    e = _best_entry(q)
    if e is not None:
        return _entry_answer(e)
    stations = entities.get("stations", [])
    if profiles and len(stations) == 1 and _STATION_PROFILE_RE.search(question):
        s = next((x for x in kb["stations"] if x["key"] == stations[0]), None)
        if s:
            return _station_answer(s)
    if _has_alias(q, kb["kpcl"]["aliases"]) and not stations:
        return _kpcl_answer(question)
    return _glossary_answer(question)


def _entry_aliases() -> list[str]:
    kb = _kb()
    return sorted((a for g in ENTRY_GROUPS for e in kb.get(g, []) for a in e["aliases"]),
                  key=len, reverse=True)


@lru_cache(maxsize=1)
def _station_names() -> list[str]:
    return sorted({_norm(s["name"]).strip() for s in _kb()["stations"]}, key=len, reverse=True)


def _without_entry_names(question: str) -> str:
    """The question with regulator / project / company / full station names removed, so "pumped STORAGE project" or
    "Sharavathy GENERATING Station" is not read as a measure when deciding whether the question asks for data."""
    q = _norm(question)
    for a in _entry_aliases() + _station_names():
        q = q.replace(_norm(a), " ")
    return q.strip()


def _asks_for_data(question: str) -> tuple[bool, dict]:
    """(asks for data, entities). Stations come from the whole question; measures and periods from what is left
    once the names are removed."""
    rest = _without_entry_names(question)
    left = extract_entities(rest) if rest else {}
    entities = extract_entities(question)
    if left.get("periods") or left.get("metrics") or left.get("units") or len(entities.get("stations", [])) > 1:
        return True, entities
    if is_about_organisation(question):
        return False, entities       # "How many full-time directors are there?", "List the board committees"
    return bool(_DATA_INTENT_RE.search(rest)), entities


_DEFINITION_RE = re.compile(r"^\s*(what is|what's|whats|what are|who is|who are|tell me about|explain|describe|define|"
                            r"meaning of|what does)\s+(the |an |a )?((role|purpose|function|job|work) of (the )?)?"
                            r"(?P<subject>.+?)\s*(stand for|mean|do)?\s*\??\s*$", re.I)


def is_definition(question: str) -> bool:
    """"What is KERC?" -- a plain request to explain one thing the knowledge base names, which should get the
    explanation even if a capability note also mentions that thing."""
    m = _DEFINITION_RE.match(question or "")
    if not m:
        return False
    subject = _norm(m.group("subject"))
    kb = _kb()
    names = _entry_aliases() + kb["kpcl"]["aliases"] + [s["key"] for s in kb["stations"]] + list(kb["glossary"])
    return any(subject == _norm(n) for n in names)


def primary(question: str) -> str | None:
    """A general question with no data intent, answered from the knowledge base."""
    q = (question or "").strip()
    if not q:
        return None
    asks, entities = _asks_for_data(q)
    if asks:
        return None
    if not _GENERAL_RE.search(q) and len(q.split()) > 4 and not is_about_organisation(q):
        return None
    return _match(q, entities, profiles=True)


def fallback(question: str) -> str | None:
    """Used when the warehouse can't answer: any clear knowledge-base match, but never for a question that asks for
    data (a description of RTPS is not an answer to "RTPS PLF in March 2024")."""
    q = (question or "").strip()
    if not q:
        return None
    asks, entities = _asks_for_data(q)
    if asks:
        return None
    return _match(q, entities, profiles=True)
