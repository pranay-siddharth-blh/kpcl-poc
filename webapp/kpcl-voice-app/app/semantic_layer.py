"""
Loads kpcl_semantic_layer/semantic_layer.json (read-only) and turns a question into entities with plain
regex / dictionary matching -- no LLM, no embeddings, no database call -- so this step is never a latency
source. The entities feed the metric lookup, the prompt (as verified codes), the SQL guards and the
answer writer.

Entities (all optional):
  stations [keys], units [keys], reservoirs [names], category (dashboard label), plant_type, scope
  ('company' for KPCL-wide questions), periods [{kind, value, fy, label, relative}], metrics [keys],
  intents [ranking, comparison, breakdown, trend], as_of (the data end, for 'now' questions)
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import SEMANTIC_LAYER_PATH

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_WORD = r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


@lru_cache(maxsize=1)
def load_semantic_layer() -> dict:
    return json.loads(SEMANTIC_LAYER_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def column_info() -> dict[str, dict]:
    """Column name -> {unit, additive, doc} merged over all mart tables (first definition wins)."""
    out: dict[str, dict] = {}
    for t in load_semantic_layer()["tables"].values():
        for c, info in t["columns"].items():
            out.setdefault(c, info)
    return out


def data_as_of() -> str:
    return load_semantic_layer()["data_period"]["data_as_of"]


def norm(text: str) -> str:
    """Lower case, curly quotes straightened, runs of spaces collapsed; hyphens kept (station codes use them)."""
    s = text.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return " ".join(s.split())


# ---------------------------------------------------------------------------------------------- router index
@dataclass
class RouterIndex:
    stations: dict[str, list[str]] = field(default_factory=dict)       # key -> [lower-case names/aliases]
    station_names: dict[str, str] = field(default_factory=dict)        # key -> display name
    station_type: dict[str, str] = field(default_factory=dict)         # key -> plant_type_code
    units: set[str] = field(default_factory=set)
    reservoirs: dict[str, list[str]] = field(default_factory=dict)     # name -> [aliases]
    reasons: dict[str, list[str]] = field(default_factory=dict)        # outage / back-down reason code -> [phrases]
    suppliers: dict[str, list[str]] = field(default_factory=dict)      # coal supplier code -> [phrases]
    categories: dict[str, list[str]] = field(default_factory=dict)     # label -> [aliases]
    fiscal_years: set[str] = field(default_factory=set)
    water_years: set[str] = field(default_factory=set)
    month_labels: set[str] = field(default_factory=set)
    metric_synonyms: list[tuple[str, str]] = field(default_factory=list)  # (synonym, metric key), longest first
    date_from: str = ""
    date_to: str = ""


@lru_cache(maxsize=1)
def router_index() -> RouterIndex:
    layer = load_semantic_layer()
    ents = layer["entities"]
    idx = RouterIndex()
    for s in ents["stations"]:
        names = {s["key"].lower(), s["name"].lower(), *[a.lower() for a in ents["station_aliases"].get(s["key"], [])]}
        idx.stations[s["key"]] = sorted(names, key=len, reverse=True)
        idx.station_names[s["key"]] = s["name"]
        idx.station_type[s["key"]] = s["plant_type"]
    idx.units = {u["key"] for u in ents["units"]}
    for r in ents["reservoirs"]:
        idx.reservoirs[r["name"]] = sorted({r["name"].lower(), *ents["reservoir_aliases"].get(str(r["key"]), [])},
                                           key=len, reverse=True)
    idx.categories = {k: sorted(v, key=len, reverse=True) for k, v in ents["category_aliases"].items()}
    # Reason phrases as people write them: the description ("Shear pin failure", "Equipment / system constraints"),
    # its parts around a slash, and the code with spaces ("no load demand"). "Others" is too vague to match.
    for r in ents.get("reasons", []):
        if r["code"] == "OTHERS":
            continue
        desc = r["description"].lower()
        names = {desc, r["code"].lower().replace("_", " "), *[p.strip() for p in desc.split("/") if len(p.strip()) > 4]}
        idx.reasons[r["code"]] = sorted(names, key=len, reverse=True)
    # Coal suppliers: "Mahanadi Coalfields", "Mahanadi", "MCL"; "imported coal" / "imported".
    for s in ents.get("suppliers", []):
        name = s["name"].lower()
        names = {s["code"].lower(), name, name.replace(" ltd", "").strip(), name.split()[0]}
        if s["code"] == "IMPORTED":
            names = {"imported coal", "imported", "import coal", "foreign coal"}
        idx.suppliers[s["code"]] = sorted(names, key=len, reverse=True)
    idx.fiscal_years = set(ents["fiscal_years"])
    idx.water_years = set(ents["water_years"])
    idx.month_labels = set(ents["month_labels"])
    syn = [(s.lower(), m["key"]) for m in layer["metrics"] for s in m["synonyms"]]
    idx.metric_synonyms = sorted(syn, key=lambda x: len(x[0]), reverse=True)
    idx.date_from = layer["data_period"]["data_from"]
    idx.date_to = layer["data_period"]["data_to"]
    return idx


def _word_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


# ---------------------------------------------------------------------------------------------- periods
def _fy_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def _month_label(year: int, month: int) -> str:
    return f"{_MONTHS[month - 1].upper()}-{year}"


def _month_no(word: str) -> int:
    return _MONTHS.index(word[:3].lower()) + 1


def _year4(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 100 else y


def _fy_of(year: int, month: int) -> str:
    return _fy_label(year if month >= 4 else year - 1)


_QUARTER_WORDS = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4", "1st": "Q1", "2nd": "Q2", "3rd": "Q3",
                  "4th": "Q4", "last": "Q4"}
_SEASON_WORDS = [("post-monsoon", "POST_MONSOON"), ("post monsoon", "POST_MONSOON"), ("monsoon", "MONSOON"),
                 ("summer", "SUMMER"), ("winter", "WINTER")]
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"]


def _period(kind: str, value: str, label: str, fy: str | None = None, relative: str | None = None) -> dict:
    return {"kind": kind, "value": value, "fy": fy, "label": label, "relative": relative}


def extract_periods(q: str, idx: RouterIndex) -> tuple[list[dict], str]:
    """Return (periods, text with the matched spans blanked out)."""
    periods: list[dict] = []
    text = q

    def take(m: re.Match) -> None:
        nonlocal text
        text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]

    def add_date(y: int, mo: int, d: int, m: re.Match) -> None:
        try:
            day = dt.date(y, mo, d)
        except ValueError:
            return
        iso = day.isoformat()
        if idx.date_from <= iso <= idx.date_to:
            periods.append(_period("date", iso, day.strftime("%d-%b-%Y"), fy=_fy_of(y, mo)))
        else:
            periods.append(_period("out_of_range", iso, day.strftime("%d-%b-%Y")))
        take(m)

    # relative phrases first (resolved against the data end, not the wall clock)
    end = dt.date.fromisoformat(idx.date_to)
    rel = [
        (r"\b(this|current)\s+(fy|financial year|fiscal year|year)\b", lambda: _period(
            "fy", _fy_of(end.year, end.month), f"FY {_fy_of(end.year, end.month)} (April 2025 only)", relative="this FY")),
        (r"\b(last|previous|past)\s+(fy|financial year|fiscal year|year)\b", lambda: _period(
            "fy", _fy_label(int(_fy_of(end.year, end.month)[:4]) - 1), f"FY {_fy_label(int(_fy_of(end.year, end.month)[:4]) - 1)}",
            relative="last FY")),
        (r"\b(this|current)\s+(water year|wy)\b", lambda: _period(
            "water_year", _fy_label(end.year if end.month >= 6 else end.year - 1),
            f"water year {_fy_label(end.year if end.month >= 6 else end.year - 1)}", relative="this water year")),
        (r"\b(last|previous)\s+(water year|wy)\b", lambda: _period(
            "water_year", _fy_label((end.year if end.month >= 6 else end.year - 1) - 1),
            f"water year {_fy_label((end.year if end.month >= 6 else end.year - 1) - 1)}", relative="last water year")),
        (r"\b(this|current)\s+month\b", lambda: _period(
            "month", _month_label(end.year, end.month), f"{_MONTH_NAMES[end.month - 1]} {end.year}",
            fy=_fy_of(end.year, end.month), relative="this month")),
        (r"\b(last|previous)\s+month\b", lambda: (lambda d: _period(
            "month", _month_label(d.year, d.month), f"{_MONTH_NAMES[d.month - 1]} {d.year}", fy=_fy_of(d.year, d.month),
            relative="last month"))(end.replace(day=1) - dt.timedelta(days=1))),
        (r"\btoday\b", lambda: _period("date", end.isoformat(), end.strftime("%d-%b-%Y"), fy=_fy_of(end.year, end.month),
                                       relative="today")),
        (r"\byesterday\b", lambda: (lambda d: _period("date", d.isoformat(), d.strftime("%d-%b-%Y"),
                                                      fy=_fy_of(d.year, d.month), relative="yesterday"))(end - dt.timedelta(days=1))),
    ]
    for pat, make in rel:
        for m in list(re.finditer(pat, text)):
            periods.append(make())
            take(m)

    # water year 2024-25
    for m in list(re.finditer(r"\b(?:water\s*year|wy)\s*'?(20\d{2}|\d{2})\s*[-/]\s*(\d{2}|20\d{2})\b", text)):
        y = _year4(m.group(1))
        periods.append(_period("water_year", _fy_label(y), f"water year {_fy_label(y)}"))
        take(m)
    # dates: 2025-04-06 / 06-Apr-2025 / 6 April 2025 / April 6, 2025 / 06/04/2025 (day first)
    for m in list(re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)):
        add_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), m)
    # (a number right after "unit" is a unit number: "RTPS unit 8 March 2025" is March, not 8 March)
    for m in list(re.finditer(r"(?<!unit )(?<!unit-)(?<!unit#)(?<!\bu)\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]*" + _MONTH_WORD + r"[\s\-/.,']*(20\d{2}|\d{2})\b", text)):
        add_date(_year4(m.group(3)), _month_no(m.group(2)), int(m.group(1)), m)
    for m in list(re.finditer(_MONTH_WORD + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", text)):
        add_date(int(m.group(3)), _month_no(m.group(1)), int(m.group(2)), m)
    for m in list(re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", text)):
        add_date(int(m.group(3)), int(m.group(2)), int(m.group(1)), m)
    # FY: "FY 2024-25", "FY24-25", "financial year 2024-2025", bare "2024-25"
    for m in list(re.finditer(r"\b(?:fy|financial year|fiscal year)?\s*'?(20\d{2}|\d{2})\s*[-/]\s*(20\d{2}|\d{2})\b(?![-/]\d)", text)):
        a, b = _year4(m.group(1)), _year4(m.group(2))
        if b == a + 1:
            periods.append(_period("fy", _fy_label(a), f"FY {_fy_label(a)}"))
            take(m)
        elif m.group(0).strip()[:1].isdigit() and len(m.group(1)) == 4 and 1 <= int(m.group(2)) <= 12:
            periods.append(_period("month", _month_label(a, int(m.group(2))), f"{_MONTH_NAMES[int(m.group(2)) - 1]} {a}",
                                   fy=_fy_of(a, int(m.group(2)))))
            take(m)
    # "FY25" / "FY 2025" = the FY ending in March 2025
    for m in list(re.finditer(r"\bfy\s*'?(20\d{2}|\d{2})\b", text)):
        end_year = _year4(m.group(1))
        periods.append(_period("fy", _fy_label(end_year - 1), f"FY {_fy_label(end_year - 1)}"))
        take(m)
    # months: "March 2025", "Mar-2025", "MAR-25", "Mar'25"
    for m in list(re.finditer(_MONTH_WORD + r"[\s\-',]*(20\d{2}|\d{2})\b", text)):
        y, mo = _year4(m.group(2)), _month_no(m.group(1))
        periods.append(_period("month", _month_label(y, mo), f"{_MONTH_NAMES[mo - 1]} {y}", fy=_fy_of(y, mo)))
        take(m)

    # a season of a calendar year: "the 2022 monsoon", "monsoon 2022", "summer of 2019"
    for word, season in _SEASON_WORDS:
        for m in list(re.finditer(r"\b(?:(20\d{2})\s+" + word + r"|" + word + r"\s+(?:of\s+|in\s+)?(20\d{2}))\b", text)):
            y = int(m.group(1) or m.group(2))
            periods.append(_period("season_cy", season, f"the {season.lower().replace('_', '-')} months of {y}",
                                   fy=str(y)))
            take(m)
    # calendar year: "calendar year 2019", "CY 2019", "in 2019" (a bare year; FY-style ranges were taken above)
    for m in list(re.finditer(r"\b(?:calendar year|cy)\s*(20\d{2})\b|\b(?:in|during|for|of)\s+(20\d{2})\b(?!\s*[-/]\s*\d)", text)):
        y = int(m.group(1) or m.group(2))
        periods.append(_period("calendar_year", str(y), f"calendar year {y} (Jan-Dec)"))
        take(m)

    fys = [p["value"] for p in periods if p["kind"] == "fy"]
    # quarters and seasons attach to the (single) FY named in the question
    for m in list(re.finditer(r"\b(q[1-4])\b|\b(first|second|third|fourth|1st|2nd|3rd|4th|last)\s+quarter\b", text)):
        qv = (m.group(1) or _QUARTER_WORDS[m.group(2)]).upper()
        fy = fys[0] if len(fys) == 1 else None
        periods.append(_period("quarter", qv, f"{qv} of FY {fy}" if fy else qv, fy=fy))
        take(m)
    for word, season in _SEASON_WORDS:
        for m in list(re.finditer(r"\b" + word + r"\b", text)):
            fy = fys[0] if len(fys) == 1 else None
            periods.append(_period("season", season, f"the {season.lower().replace('_', '-')} of FY {fy}" if fy
                                   else f"the {season.lower().replace('_', '-')}", fy=fy))
            take(m)
    # validate against the data
    for p in periods:
        if p["kind"] == "fy" and p["value"] not in idx.fiscal_years:
            p["kind"] = "out_of_range"
        elif p["kind"] == "month" and p["value"] not in idx.month_labels:
            p["kind"] = "out_of_range"
        elif p["kind"] == "water_year" and p["value"] not in idx.water_years:
            p["kind"] = "out_of_range"
        elif p["kind"] in ("calendar_year", "season_cy"):
            y = int(p["value"] if p["kind"] == "calendar_year" else p["fy"])
            if not int(idx.date_from[:4]) <= y <= int(idx.date_to[:4]):
                p["kind"] = "out_of_range"
    # a quarter or season names its FY; drop the separate FY entry so there is one period
    if any(p["kind"] in ("quarter", "season") and p["fy"] for p in periods):
        periods = [p for p in periods if not (p["kind"] == "fy" and any(
            x["kind"] in ("quarter", "season") and x["fy"] == p["value"] for x in periods))]
    return periods, text


# ---------------------------------------------------------------------------------------------- places
_RESERVOIR_WORDS = re.compile(r"\b(dam|dams|reservoirs?|level|levels|storage|inflows?|frl|mddl|spill|rainfall|"
                              r"water level|live storage|capacity %|full reservoir|draw ?down)\b")
_WATER_WORDS = re.compile(r"\b(reservoirs?|level|levels|storage|inflows?|frl|mddl|spill\w*|sluice|rainfall|rain|"
                          r"water level|live storage|full reservoir|draw ?down|how full|percent full|energy potential|"
                          r"discharge\w*|filled)\b")
_GENERATION_WORDS = re.compile(r"\b(generat\w*|plf|paf|power house|station|units?|output|mu|trips?|outages?|"
                               r"aux\w*|net export|target)\b")
_UNIT_RE_TMPL = r"(?<![a-z0-9]){name}[\s-]*(?:unit|u)[\s#-]*(\d{{1,2}})(?![0-9])"
_UNIT_OF_RE = re.compile(r"\bunit[\s#-]*(\d{1,2})\s+(?:of|at|in)\s+")
_COMPANY_RE = re.compile(r"\b(kpcl|company|corporation|all stations|all plants|overall|in total|total generation|"
                         r"company-wide|across all)\b")


def extract_places(q: str, idx: RouterIndex) -> dict:
    out: dict = {}
    units: list[str] = []
    for key, names in idx.stations.items():
        for name in names:
            for m in re.finditer(_UNIT_RE_TMPL.format(name=re.escape(name)), q):
                uk = f"{key}-U{int(m.group(1))}"
                if uk in idx.units and uk not in units:
                    units.append(uk)
            for m in _UNIT_OF_RE.finditer(q):
                if _word_re(name).match(q, m.end()):
                    uk = f"{key}-U{int(m.group(1))}"
                    if uk in idx.units and uk not in units:
                        units.append(uk)
    stations: list[tuple[int, str]] = []
    rest = q  # station names are blanked out of this copy so words inside them ("Raichur Thermal") aren't re-read
    for key, names in idx.stations.items():
        for name in names:
            m = _word_re(name).search(q)
            if m:
                stations.append((m.start(), key))
                for mm in _word_re(name).finditer(rest):
                    rest = rest[:mm.start()] + " " * (mm.end() - mm.start()) + rest[mm.end():]
                break
    reservoirs: list[tuple[int, str]] = []
    for rname, aliases in idx.reservoirs.items():
        for a in aliases:
            m = _word_re(a).search(q)
            if m:
                reservoirs.append((m.start(), rname))
                break
    # A name shared by a dam and a power house (Supa, Mani, Gerusoppa, Alamatti, Linganamakki) is the dam when the
    # question is about levels / storage, or says "dam" / "reservoir" right next to it.
    res_names = {r for _, r in reservoirs}
    if res_names:
        dam_next = any(re.search(re.escape(a) + r"\s+(dam|reservoir)\b|\b(dam|reservoir)\s+" + re.escape(a), q)
                       for r in res_names for a in idx.reservoirs[r])
        about_water = bool(_WATER_WORDS.search(q))       # levels, storage, inflow ... ('dam' alone is not enough)
        about_power = bool(_GENERATION_WORDS.search(q))
        keep_res = about_water or (dam_next and not about_power)
        if keep_res:
            shared = {"SUPA": "SUPA", "MANI": "MANI", "GERUSOPPA": "GERUSOPPA", "ALAMATTI": "ALAMATTI"}
            dam_station = {shared[r] for r in res_names if r in shared}
            if not dam_next or about_water:
                # keep the power house only if the question calls it that ("Supa power house generation")
                stations = [(p, k) for p, k in stations if k not in dam_station or any(
                    re.search(re.escape(n) + r"\s+(power house|station|ph|dph|generation)", q) for n in idx.stations[k])]
            out["reservoirs"] = [r for _, r in sorted(reservoirs)]
    for uk in units:
        st = uk.rsplit("-U", 1)[0]
        if st not in [k for _, k in stations]:
            stations.append((q.find(st.lower()) if st.lower() in q else 999, st))
    if stations:
        out["stations"] = [k for _, k in sorted(set(stations))]
    if units:
        out["units"] = units
    elif len(out.get("stations", [])) == 1:
        m = re.search(r"\b(?:unit|u)[\s#-]*(\d{1,2})\b", q)
        if m:
            uk = f"{out['stations'][0]}-U{int(m.group(1))}"
            if uk in idx.units:
                out["units"] = [uk]
    if "stations" not in out:
        for label, aliases in idx.categories.items():
            if any(_word_re(a).search(rest) for a in aliases):
                out.setdefault("categories", []).append(label)
        if _word_re("hydro").search(rest) and not {"MAJOR HYDRO", "MINOR HYDRO"} & set(out.get("categories", [])):
            out["plant_type"] = "HYDRO"
    if not any(k in out for k in ("stations", "units", "categories", "reservoirs", "plant_type")):
        if _COMPANY_RE.search(q):
            out["scope"] = "company"
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------------------------------------- metrics, intents
_INTENTS = {
    "ranking": re.compile(r"\b(which|top|highest|lowest|best|worst|most|least|maximum|minimum|max|min|rank\w*|leading|"
                          r"largest|smallest|biggest|record|peak day|best day)\b"),
    "comparison": re.compile(r"\b(compare\w*|comparison|vs\.?|versus|against|difference|differ|compared|than)\b"),
    "breakdown": re.compile(r"\b(by|each|every|per|breakdown|split|all units|all stations|list|wise|unit-wise|"
                            r"station-wise|month-wise|category-wise|reason-wise|individual)\b"),
    "trend": re.compile(r"\b(trend\w*|over time|month on month|year on year|yoy|growth|grew|over the years|history|"
                        r"historical|monthly|yearly|annual trend|every month|every year|each month|each year|since)\b"),
}


def extract_metrics(q: str, idx: RouterIndex) -> list[str]:
    text = q
    found: list[tuple[int, str]] = []
    for syn, key in idx.metric_synonyms:
        for m in list(_word_re(syn).finditer(text)):
            found.append((m.start(), key))
            text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    out: list[str] = []
    for _, key in sorted(found):
        if key not in out:
            out.append(key)
    # "did SGS meet / miss / achieve its CEA target" asks for achievement, not the target itself
    if "cea_target" in out and _TARGET_MET_RE.search(q):
        out[out.index("cea_target")] = "cea_target_achievement"
        out = list(dict.fromkeys(out))
    return out


_TARGET_MET_RE = re.compile(r"\b(meet|meets|met|achiev\w*|miss\w*|short|shortfall|exceed\w*|beat|surpass\w*|reach\w*)\b")


def extract_entities(question: str, idx: RouterIndex | None = None) -> dict:
    idx = idx or router_index()
    q = norm(question)
    periods, rest = extract_periods(q, idx)
    entities: dict = extract_places(rest, idx)
    reasons = [code for code, names in idx.reasons.items() if any(_word_re(n).search(rest) for n in names)]
    if reasons:
        entities["reasons"] = reasons
    suppliers = [code for code, names in idx.suppliers.items() if any(_word_re(n).search(rest) for n in names)]
    if suppliers and re.search(r"\b(coal|supplier|suppliers|coalfields?|collier\w*|rakes?|received|receipts?|imported)\b", rest):
        entities["suppliers"] = suppliers
    metrics = extract_metrics(rest, idx)
    if metrics:
        entities["metrics"] = metrics
    if periods:
        entities["periods"] = periods
    intents = [name for name, rx in _INTENTS.items() if rx.search(q)]
    if intents:
        entities["intents"] = intents
    if re.search(r"\b(now|currently|current(ly)?|at present|right now|as of now|latest|presently|still)\b", q):
        entities["as_of"] = idx.date_to
    return entities


# ---------------------------------------------------------------------------------------------- pruning
_GROUP_WORDS = {
    "outage": r"\b(outages?|trips?|tripp\w*|btf|btl|tube|breakdowns?|shut ?downs?|forced|planned|rsd|reserve shut|down|"
              r"not running|out of service|failures?|leaks?|overhaul)\b",
    "backdown": r"\b(back-?\s?down|backdown|loss|losses|lost|lose|losing|nld|no load|curtail\w*)\b",
    "coal": r"\b(coal|rakes?|stock|suppliers?|mcl|sccl|wcl|imported|gcv|calorific)\b",
    "reservoir": r"\b(reservoirs?|dams?|levels?|storage|inflows?|rainfall|spill\w*|frl|mddl|sluice|discharge)\b",
    "grid": r"\b(grid|state|consumption|demand|import\w*|frequency|system peak|share|contribution|karnataka)\b",
    "status": r"\b(currently|now|running|status|since|cumulative|life|lifetime|commission\w*|first synchron\w*|still)\b",
    "target": r"\b(targets?|cea|achiev\w*|shortfall)\b",
}
_METRIC_GROUP = {"backdown_loss": "backdown", "coal_below_norm_days": "coal", "coal_receipt": "coal",
                 "forced_outage_count": "outage", "btf_count": "outage", "kpcl_share_of_consumption": "grid",
                 "cea_target": "target", "cea_target_achievement": "target", "internal_target": "target",
                 **{k: "grid" for k in ("state_consumption", "state_generation", "state_import", "system_peak",
                                        "min_frequency", "max_frequency", "tb_share", "jurala_share")},
                 **{k: "reservoir" for k in ("reservoir_level", "storage_pct", "energy_potential", "inflow", "rainfall",
                                             "spill", "discharge_power")}}
_ALWAYS_TABLES = ["mart_dim_station", "mart_dim_unit"]


def relevant_groups(question: str, entities: dict) -> list[str]:
    q = norm(question)
    groups = {g for g, rx in _GROUP_WORDS.items() if re.search(rx, q)}
    groups |= {_METRIC_GROUP[m] for m in entities.get("metrics", []) if m in _METRIC_GROUP}
    if entities.get("reservoirs"):
        groups.add("reservoir")
    if entities.get("reasons"):
        groups |= {"backdown", "outage"}
    gen_metrics = [m for m in entities.get("metrics", []) if m not in _METRIC_GROUP]
    if gen_metrics or not groups or groups & {"target", "grid"} or entities.get("stations") or entities.get("units"):
        groups.add("generation")
    if "status" in groups:
        groups.add("outage")
    return sorted(groups)


def prune_semantic_layer(question: str, entities: dict) -> dict:
    """The subset of the layer that goes into the prompt: the tables of the relevant groups, the metrics named
    (plus the headline ones), the glossary terms mentioned, all rules."""
    layer = load_semantic_layer()
    q = norm(question)
    groups = relevant_groups(question, entities)
    tables = {n: t for n, t in layer["tables"].items() if t["group"] in groups or n in _ALWAYS_TABLES}
    if entities.get("reservoirs") or "reservoir" in groups:
        tables["mart_dim_reservoir"] = layer["tables"]["mart_dim_reservoir"]
    if re.search(r"\b(week|weekday|weekend|sunday|monday|saturday|day of the week)\b", q):
        tables["mart_dim_date"] = layer["tables"]["mart_dim_date"]
    named = set(entities.get("metrics", []))
    metrics = [m for m in layer["metrics"] if m["key"] in named or m["key"] in ("gross_generation", "plf")]
    glossary = {t: g["plain"] for t, g in layer["glossary"].items() if any(_word_re(a).search(q) for a in g["aliases"])}
    return {"groups": groups, "tables": tables, "metrics": metrics, "glossary": glossary, "rules": layer["rules"],
            "data_period": layer["data_period"]}


# ---------------------------------------------------------------------------------------------- unknown names
_KNOWN_ACRONYMS = {
    "KPCL", "RPCL", "KPCGPCL", "PLF", "PAF", "NLD", "RSD", "BTF", "BTL", "GCV", "HFO", "LDO", "MPR", "JMR", "CEA", "KERC",
    "ESCOM", "ESCOMS", "FY", "WY", "MU", "MW", "KWH", "MWH", "GWH", "HO", "RES", "WTE", "MCL", "SCCL", "WCL", "KPTCL",
    "BESCOM", "MESCOM", "CESCOM", "CESC", "HESCOM", "GESCOM", "OAF", "UCB", "DMS", "PTW", "MDDL", "FRL", "AUX", "SCC",
    "GHR", "MMSCM", "MSW", "CCGT", "PV", "WTG", "YOY", "MTD", "YTD", "DPH", "SECI", "GOK", "HRMS", "ERP", "AVG", "MAX",
    "MIN", "THE", "AND", "ALL", "TOTAL", "SLDC", "NLDC", "LNG", "MT", "KL", "MCFT", "COAL", "GAS", "HYDRO", "THERMAL",
    "SOLAR", "WIND", "MAJOR", "MINOR", "UNIT", "UNITS", "PDF", "CSV", "IST", "AM", "PM", "NOW", "WHAT", "WHICH", "HOW",
    "SHOW", "LIST", "GIVE", "TELL", "IMPORTED", "DOMESTIC", "FORCED", "PLANNED", "APR", "MAR", "MAY", "JUN", "JUL",
    "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC", "JAN", "FEB", "GMS", "EMR", "TPS", "HEP", "DPH", "CPP", "GS", "PH",
    "K-ERP", "HO-DAILY",
}
_CODE_TOKEN_RE = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*)(?![\w-])")
_CALLED_RE = re.compile(r"\b(?:station|plant|unit|project|power house|dam|reservoir)\s+(?:called|named)\s+['\"]?([A-Za-z][\w-]*)",
                        re.IGNORECASE)


def unknown_names(question: str, idx: RouterIndex | None = None) -> list[str]:
    """Station / unit-like codes in the question that are not in the data ('ZZTPS', 'RTPS-U12', 'a plant called X').
    A question naming one must not be answered for KPCL as a whole or for some other station."""
    idx = idx or router_index()
    known = {k.upper() for k in idx.stations} | {u.upper() for u in idx.units} | _KNOWN_ACRONYMS
    known |= {a.upper() for names in idx.stations.values() for a in names} | {r.upper() for r in idx.reservoirs}
    known |= {a.upper() for names in idx.reservoirs.values() for a in names}
    out = []
    letters = [c for c in question if c.isalpha()]
    shouting = letters and sum(c.isupper() for c in letters) / len(letters) > 0.6
    for tok in ([] if shouting else _CODE_TOKEN_RE.findall(question)):
        if tok in known or re.fullmatch(r"[A-Z]{3}-\d{2,4}", tok) or re.fullmatch(r"Q[1-4]|H[12]", tok):
            continue
        if re.fullmatch(r"(FY|WY|CY)'?\d{2,4}(-\d{2,4})?", tok):
            continue  # "FY24", "FY2024-25": a period, not a station
        if not (re.search(r"[\d-]", tok) or re.search(r"(TPS|PH|GS|EP|PP|HE|HEP|WEF)$", tok)):
            continue  # an ordinary capitalised word, not a station-like code
        base = tok.rsplit("-U", 1)[0] if re.search(r"-U\d+$", tok) else None
        if base and base in known and tok not in known:
            out.append(tok)  # a unit number the station doesn't have
        elif not base and not tok.isdigit():
            out.append(tok)
    out += [u for u in bad_unit_refs(question, idx) if u not in out]
    for name in _CALLED_RE.findall(question):
        low = name.lower()
        if not any(low == a or low in a.split() for names in list(idx.stations.values()) + list(idx.reservoirs.values())
                   for a in names):
            out.append(name)
    return list(dict.fromkeys(out))


def bad_unit_refs(question: str, idx: RouterIndex | None = None) -> list[str]:
    """'RTPS unit 12', 'unit 9 of BTPS': a unit number the named station does not have -> ['RTPS-U12']."""
    idx = idx or router_index()
    q = norm(question)
    out = []
    for key, names in idx.stations.items():
        for name in names:
            pats = [_UNIT_RE_TMPL.format(name=re.escape(name)),
                    r"\bunit[\s#-]*(\d{1,2})\s+(?:of|at|in)\s+(?:the\s+)?" + re.escape(name) + r"(?![a-z0-9])"]
            for pat in pats:
                for m in re.finditer(pat, q):
                    uk = f"{key}-U{int(m.group(1))}"
                    if uk not in idx.units and uk not in out:
                        out.append(uk)
    return out


def station_unit_count(station: str) -> int:
    return sum(1 for u in router_index().units if u.rsplit("-U", 1)[0] == station)


# ---------------------------------------------------------------------------------------------- simple intents
_IDENTITY_RE = re.compile(r"\b(who\s+are\s+you|what\s+are\s+you|what\s*(?:'s|s|\s+is)\s+your\s+name|what\s+can\s+you\s+do|"
                          r"who\s+(?:made|created|built|developed)\s+you|are\s+you\s+(?:a\s+|an\s+)?(?:bot|robot|ai|chatbot|human)|"
                          r"tell\s+me\s+about\s+yourself|how\s+can\s+you\s+help|^\s*(hi|hello|hey|namaste|help)\b)", re.I)
_DEFINITION_RE = re.compile(r"^\s*(?:what\s*(?:'s|s|\s+is|\s+are|\s+does)|define|explain|meaning\s+of|tell\s+me\s+about)\s+"
                            r"(?:the\s+|a\s+|an\s+)?(?:term\s+)?(?P<subject>.+?)(?:\s+mean|\s+stand\s+for)?\s*\??\s*$", re.I)


def is_identity_question(question: str) -> bool:
    return bool(_IDENTITY_RE.search(question))


def definitional_term(question: str) -> str | None:
    """'What is PLF?' -> 'PLF'. Only when the whole subject is a glossary term or alias, so 'What is RTPS PLF in
    March 2025?' stays a data question."""
    m = _DEFINITION_RE.match(question)
    if not m:
        return None
    subject = re.sub(r"[^a-z0-9%+ -]+", " ", m.group("subject").lower())
    subject = " ".join(subject.replace("full form of", "").replace("meaning of", "").split())
    for term, g in load_semantic_layer()["glossary"].items():
        if subject == term.lower() or subject in [a.lower() for a in g["aliases"]]:
            return term
    return None
