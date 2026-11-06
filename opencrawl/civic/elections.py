"""Locally run election results and qualifying lists.

Many municipal elections never reach a state results system: the results and the
qualifying (candidate) lists live on city and county election pages as HTML tables
and PDFs. This module:

  seed       finds the county/city elections office site (portals.elections) so it
             is crawled as its own source (`<id>:elections`)
  classify   marks crawled pages/documents that are results or qualifying lists
  derive     parses them into contests:
               election_result  one per contest: date, office, candidates with votes,
                                percentages and outcomes (won / runoff / led / unopposed)
               qualifying       one per list: office -> qualified candidates
  export     an archive shape (elections with x-prefixed eids, contests with
             `o: [name, party, votes, incumbent, outcome]`) so the site can merge them,
             and councils shape for the city council view

Every contest keeps the url of the page or document it was read from.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from ..derive import Deriver, register
from ..store import Record

RESULTS_RX = re.compile(r"\b(election\s+results?|results?\s+of\s+the\s+.{0,40}election|unofficial\s+results|official\s+results|"
                        r"certified\s+results|election\s+summary|summary\s+report|canvass|returns?\b.{0,20}election|"
                        r"runoff\s+results?|vote\s+totals?)", re.I)
QUALIFYING_RX = re.compile(r"\b(qualif(ied|ying)\s+candidates?|candidates?\s+(who\s+)?qualif(ied|ying)|notice\s+of\s+candidates?|"
                           r"list\s+of\s+candidates|candidate\s+qualifying|qualifying\s+(list|report|period))", re.I)
OFFICE_RX = re.compile(r"^(?:(?:city|town|village|borough)\s+of\s+[\w .'-]+?\s*[-–:]\s*)?(mayor|council|councilm[ae]n|councilmember|"
                       r"commission\w*|alderm\w*|trustee\w*|supervisor\w*|selectm[ae]n|board\s+of\s+(education|commissioners?|trustees)|"
                       r"school\s+board|post\s+\d+|ward\s+\d+|district\s+\d+|seat\s+\w+|judge|magistrate|sheriff|coroner|clerk|"
                       r"treasurer|assessor|tax\s+commissioner|solicitor|surveyor|referendum|question|ballot\s+(?:issue|measure)|"
                       r"amendment|splost|tsplost|esplost|homestead|brunch|sunday\s+sales)\b", re.I)
SKIP_ROW = re.compile(r"^(total|totals?\b|total\s+votes|votes\s+cast|undervotes?|overvotes?|write[- ]?ins?|write[- ]?in\s+totals?|"
                      r"blank|registered\s+voters?|ballots?\s+cast|turnout|precincts?\s+reporting|contest\s+total|times\s+counted)",
                      re.I)
NAME_RX = re.compile(r"^[A-Z][A-Za-z'.\-]+(?:\s+(?:\"[^\"]+\"|\([^)]+\)|[A-Z][A-Za-z'.\-]*|de|van|von|la|del))*(?:,?\s+(?:Jr|Sr|II|III|IV)\.?)?$")
VOTES_RX = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{1,6})(?![\d,])\s*(?:votes?)?\s*(?:\(?\s*(\d{1,3}(?:\.\d+)?)\s*%\)?)?\s*$")
DATE_RX = re.compile(r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+(20\d\d))\b|"
                     r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b|\b(20\d\d-\d{2}-\d{2})\b")
MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                            "september", "october", "november", "december"])}
PARTY_RX = re.compile(r"\((D|R|L|DEM|REP|LIB|IND|NP|NONPARTISAN|DEMOCRAT\w*|REPUBLICAN|LIBERTARIAN|INDEPENDENT)\)", re.I)
INCUMBENT_RX = re.compile(r"\(\s*(incumbent|inc\.?|i)\s*\)|\bincumbent\b|\*", re.I)   # "(I)" on a ballot = incumbent
OFFICE_NUMBER = re.compile(r"\b(post|ward|district|seat|place|position|precinct|no\.?|#|chapter)\s*#?\s*\d+\w*\b|\b(19|20)\d\d\b", re.I)
LIST_MARK = re.compile(r"^(?:[-*•·]|\d{1,2}[.)])\s+")


def looks_like_results(title: str, url: str, text: str) -> bool:
    head = f"{title}\n{url}\n{text[:1500]}"
    if not RESULTS_RX.search(head):
        return False
    return sum(len(c["candidates"]) for c in parse_results(text[:60000])) >= 2


def looks_like_qualifying(title: str, url: str, text: str) -> bool:
    return bool(QUALIFYING_RX.search(f"{title}\n{url}\n{text[:1500]}"))


def find_date(text: str, title: str = "") -> str | None:
    for m in DATE_RX.finditer(f"{title}\n{text[:6000]}"):
        if m.group(1):
            mon, day, year = m.group(1).replace(",", "").split()
            return f"{year}-{MONTHS[mon.lower()]:02d}-{int(day):02d}"
        if m.group(3):
            return f"{m.group(5)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
        if m.group(6):
            return m.group(6)
    return None


def _clean_name(raw: str) -> tuple[str, str, bool]:
    """-> (name, party, incumbent)"""
    s = LIST_MARK.sub("", raw.strip().strip("|").strip())
    party = ""
    m = PARTY_RX.search(s)
    if m:
        p = m.group(1).upper()
        party = {"D": "D", "R": "R", "L": "L", "DEM": "D", "REP": "R", "LIB": "L", "IND": "I", "DEMOCRAT": "D", "DEMOCRATIC": "D",
                 "REPUBLICAN": "R", "LIBERTARIAN": "L", "INDEPENDENT": "I"}.get(p, "")
        s = (s[:m.start()] + s[m.end():]).strip()
    inc = bool(INCUMBENT_RX.search(s)) or s.endswith("*")
    s = INCUMBENT_RX.sub("", s).replace("*", "").strip(" -–,")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\.{3,}|…", "", s).strip()
    return s, party, inc


YESNO = {"yes", "no", "for", "against", "yes/si", "no/no", "si"}
# Ballot question types, most specific first. Southern "local option sales tax" names (SPLOST...) sit beside the
# generic sales / use / lodging / excise taxes, mill levies and debt questions found in most states.
QUESTION_TYPES = [
    ("ESPLOST", r"\b(esplost|education(?:al)?\s+(?:special\s+purpose\s+)?local\s+option\s+sales\s+tax|school\s+(?:sales\s+)?tax)\b"),
    ("TSPLOST", r"\b(tsplost|transportation\s+(?:special\s+purpose\s+)?local\s+option\s+sales\s+tax)\b"),
    ("SPLOST", r"\b(splost|special\s+purpose\s+local\s+option\s+sales\s+tax)\b"),
    ("LOST", r"\b(lost\b|local\s+option\s+sales\s+tax)"),
    ("Homestead exemption", r"\bhomestead\s+exemption"),
    ("Bond", r"\b(general\s+obligation\s+)?bonds?\b"),
    ("Lodging / excise tax", r"\b(lodging\s+tax|occupancy\s+tax|excise\s+tax|marijuana\s+tax|tobacco\s+tax)\b"),
    ("Sales tax", r"\b(sales\s+(?:and\s+use\s+)?tax|use\s+tax)\b"),
    ("Mill levy", r"\b(mill\s+lev(?:y|ies)|mill\s+levy\s+override)\b"),
    ("Debt / revenue retention", r"\b(debt\s+(?:be\s+)?increased|retain\s+and\s+spend|revenue\s+change|de-?bruce|tabor)\b"),
    ("Alcohol", r"\b(sunday\s+sales|brunch|package\s+sales|distilled\s+spirits|malt\s+beverages|alcoholic\s+beverages|liquor|by\s+the\s+drink)\b"),
    ("Charter amendment", r"\bcharter\b"),
    ("Annexation / cityhood", r"\b(annex\w*|incorporat\w*|cityhood|de-?annex\w*|new\s+city)\b"),
    ("Redevelopment powers / TAD", r"\b(redevelopment\s+powers|tax\s+allocation\s+district)\b"),
    ("Millage / tax", r"\b(millage|property\s+tax|ad\s+valorem)\b"),
    ("Term / government structure", r"\b(term\s+limits?|city\s+manager|consolidat\w*|government\s+structure|elected\s+(?:chair|mayor))\b"),
    ("Constitutional amendment", r"\b(constitutional\s+amendment|amendment\s+\d|house\s+resolution|senate\s+resolution)\b"),
]


def question_type(text: str) -> str:
    for label, rx in QUESTION_TYPES:
        if re.search(rx, text or "", re.I):
            return label
    return "Referendum"


def is_question_contest(c: dict) -> bool:
    names = [x["name"].lower().strip() for x in c.get("candidates", [])]
    return bool(names) and all(n in YESNO for n in names)


def _is_office(line: str) -> bool:
    """An office heading: has an office word, and any numbers in it are post/ward/district numbers or years.
    A ballot question ("Shall the ...") is a heading too, whatever its length."""
    l = line.strip().strip("#").strip()
    if not l or SKIP_ROW.match(l):
        return False
    if re.match(r"^shall\b", l, re.I) and len(l) <= 600:
        return True
    if len(l) > 120 or not OFFICE_RX.search(l):
        return False
    stripped = OFFICE_NUMBER.sub("", l)
    return not re.search(r"\d", stripped)


def parse_results(text: str, runoff: bool = False) -> list[dict]:
    """Contests from results text (markdown tables or line-based PDF text). `runoff`: the page is a runoff's."""
    contests, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")] if "|" in line else [line]
        first = cells[0].strip("#").strip()
        if _is_office(first) and (len(cells) == 1 or not any(VOTES_RX.search(c) for c in cells[1:])):
            if cur and cur["candidates"]:
                contests.append(cur)
            cur = {"office": re.sub(r"\s+", " ", first).strip(" :"), "candidates": [], "runoff": runoff or "runoff" in first.lower()}
            continue
        if cur is None:
            continue
        first = LIST_MARK.sub("", first)
        if SKIP_ROW.match(first):
            if re.match(r"^(total|totals?\b|total\s+votes|votes\s+cast|contest\s+total)", first, re.I):
                m = VOTES_RX.search(line)
                if m:
                    cur["total"] = int(m.group(1).replace(",", ""))
            continue
        # a candidate row: name cell then votes (and maybe %), either in table cells or trailing on the line
        name_cell, votes, pct = None, None, None
        if len(cells) >= 2:
            name_cell = cells[0]
            for c in cells[1:]:
                m = VOTES_RX.search(c)
                if m and votes is None:
                    votes = int(m.group(1).replace(",", ""))
                    if m.group(2):
                        pct = float(m.group(2))
                elif re.fullmatch(r"\d{1,3}(\.\d+)?\s*%", c):
                    pct = float(c.rstrip("% "))
        else:
            m = VOTES_RX.search(line)
            if m:
                name_cell = line[:m.start()].strip(" .:-–")
                votes = int(m.group(1).replace(",", ""))
                pct = float(m.group(2)) if m.group(2) else None
        if name_cell is None or votes is None:
            continue
        name, party, inc = _clean_name(name_cell)
        if not name or not NAME_RX.match(name) or len(name) > 60:
            continue
        cur["candidates"].append({"name": name, "party": party, "votes": votes, "pct": pct, "incumbent": inc})
    if cur and cur["candidates"]:
        contests.append(cur)
    for c in contests:
        tot = c.get("total") or sum(x["votes"] for x in c["candidates"]) or 0
        c["total"] = tot
        for x in c["candidates"]:
            if x["pct"] is None and tot:
                x["pct"] = round(100 * x["votes"] / tot, 1)
        _outcomes(c)
    return contests


def _outcomes(c: dict):
    cands = sorted(c["candidates"], key=lambda x: -x["votes"])
    for x in c["candidates"]:
        x["outcome"] = ""
    if not cands:
        return
    if len(cands) == 1:
        cands[0]["outcome"] = "unopposed" if cands[0]["votes"] else ""
        return
    top = cands[0]
    if c.get("runoff"):
        top["outcome"] = "won"
        return
    share = (top["pct"] or 0)
    if share > 50:
        top["outcome"] = "won"
    elif share > 0:
        top["outcome"] = "led"
        if len(cands) > 1:
            cands[1]["outcome"] = "runoff" if cands[1]["votes"] else ""
            top["outcome"] = "runoff"


def parse_qualifying(text: str) -> list[dict]:
    """office -> candidates from a qualifying list (tables or lines under office headings)."""
    out, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")] if "|" in line else [line]
        first = cells[0].strip("#").strip()
        if _is_office(first):
            if cur and cur["candidates"]:
                out.append(cur)
            cur = {"office": re.sub(r"\s+", " ", first).strip(" :"), "candidates": []}
            continue
        first = LIST_MARK.sub("", first)
        if cur is None or SKIP_ROW.match(first) or first.lower() in ("name", "candidate", "candidates"):
            continue
        name, party, inc = _clean_name(first)
        if NAME_RX.match(name) and 3 <= len(name) <= 60 and not OFFICE_RX.match(name):
            cand = {"name": name, "party": party, "incumbent": inc}
            if len(cells) > 1:
                extra = " · ".join(c for c in cells[1:] if c and not VOTES_RX.search(c))[:200]
                if extra:
                    cand["note"] = extra
            cur["candidates"].append(cand)
    if cur and cur["candidates"]:
        out.append(cur)
    return out


# ----------------------------------------------------------------------------- deriver
def _derive(src: dict, ctx: dict):
    text = src.get("text") or ""
    title, url = src.get("title") or "", src.get("url") or ""
    jur = ctx.get("targets") or []
    if looks_like_results(title, url, text):
        date = find_date(text, title)
        page_runoff = bool(re.search(r"\brunoff\b", f"{title}\n{text[:800]}", re.I))
        official = bool(re.search(r"\b(certified|official)\b", f"{title} {text[:800]}", re.I)) and \
            not re.search(r"\bunofficial\b", f"{title} {text[:800]}", re.I)
        if "_muni_by_norm" not in ctx:   # once per derive run, not per contest
            idx: dict[str, str] = {}
            for j in jur:
                if j["kind"] != "county":
                    idx.setdefault(re.sub(r"[^a-z0-9]", "", j["name"].lower()), j["id"])
            ctx["_muni_by_norm"] = idx
        for i, c in enumerate(parse_results(text, runoff=page_runoff)):
            place = _contest_place(c["office"], src.get("target", ""), jur, ctx["_muni_by_norm"])
            if is_question_contest(c):
                yes = sum(x["votes"] for x in c["candidates"] if x["name"].lower() in ("yes", "for", "yes/si", "si"))
                no = sum(x["votes"] for x in c["candidates"] if x["name"].lower() in ("no", "against", "no/no"))
                yield Record(src["source_id"], "ballot_question", url, title=f"{c['office'][:160]} — {date or 'date?'}",
                             key=f"{src['source_id']}|ballot_question|{url}|{date}|{c['office'].lower()[:120]}",
                             text=f"{c['office']}\nYes: {yes}\nNo: {no}", published=date, channel="derived", target=place,
                             meta={"date": date, "question": c["office"], "type": question_type(c["office"]), "yes": yes, "no": no,
                                   "total": c.get("total") or yes + no, "result": "passed" if yes > no else "failed" if no > yes else "tie",
                                   "official": official, "contest_index": i, "page_title": title, "has_results": True})
                continue
            yield Record(src["source_id"], "election_result", url, title=f"{c['office']} — {date or 'date?'}",
                         key=f"{src['source_id']}|election_result|{url}|{date}|{c['office'].lower()}",
                         text="\n".join(f"{x['name']}: {x['votes']} ({x['pct']}%) {x['outcome']}".strip() for x in c["candidates"]),
                         published=date, channel="derived", target=place,
                         meta={"date": date, "office": c["office"], "candidates": c["candidates"], "total": c["total"],
                               "runoff": c["runoff"], "contest_index": i, "page_title": title, "official": official})
    if looks_like_qualifying(title, url, text):
        q = parse_qualifying(text)
        if q:
            date = find_date(text, title)
            yield Record(src["source_id"], "qualifying", url, title=f"Qualified candidates — {title[:80]}",
                         key=f"{src['source_id']}|qualifying|{url}",
                         text="\n".join(f"{c['office']}: " + "; ".join(x["name"] for x in c["candidates"]) for c in q),
                         published=date, channel="derived", meta={"date": date, "contests": q, "page_title": title})


def _contest_place(office: str, default: str, jurisdictions: list[dict], by_norm: dict | None = None) -> str:
    """A county results page lists municipal contests as "City of X Mayor" / "Town of X - Council": attribute to X."""
    if not jurisdictions:
        return default
    m = re.search(r"\b(?:city|town|village|borough)\s+of\s+([A-Z][\w .'-]+?)(?:\s*[-–:,]|\s+(?:mayor|council|commission|trustee|be|shall|levy|issue|impose|to)\b|\?|$)",
                  office, re.I)
    if m:
        name = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
        if by_norm is not None:
            return by_norm.get(name, default)
        for j in jurisdictions:
            if j["kind"] != "county" and re.sub(r"[^a-z0-9]", "", j["name"].lower()) == name:
                return j["id"]
    return default


register(Deriver("elections", "kind IN ('page','document') AND source_id NOT LIKE 'subj:%'", _derive))


# ----------------------------------------------------------------------------- export (archive shape)
def export_elections(store, jurisdictions: dict[str, dict]) -> list[dict]:
    """Group election_result records into archive elections: {eid, date, title, kind, official, u, c:[...]}."""
    from ..names import person_key as pkey

    groups: dict[tuple, dict] = {}
    for r in store.iter_query("kind='election_result'"):
        m = r["meta"]
        jid = r.get("target") or "state"
        date = m.get("date") or "0000-00-00"
        key = (jid, date)
        g = groups.setdefault(key, {"eid": f"x{jid}-{date}", "date": date, "year": date[:4], "target": jid,
                                   "title": f"{jurisdictions.get(jid, {}).get('name', jid)} election {date}" +
                                            (" (runoff)" if m.get("runoff") else ""),
                                   "kind": "municipal runoff" if m.get("runoff") else "municipal", "official": bool(m.get("official")),
                                   "u": r["url"], "c": [], "_seen": set()})
        sig = (m["office"].lower(), tuple(x["name"].lower() for x in m["candidates"]))
        if sig in g["_seen"]:
            continue
        g["_seen"].add(sig)
        county = ""
        j = jurisdictions.get(jid)
        if j:
            county = (j.get("counties") or [j["name"].removesuffix(" County")])[0]
        g["c"].append({"i": len(g["c"]), "lvl": "local", "sub": "", "key": m["office"], "t": m["office"], "p": "",
                       "j": county, "vf": 1, "tot": m.get("total", 0),
                       "o": [[x["name"], x.get("party", ""), x["votes"], 1 if x.get("incumbent") else 0, x.get("outcome", "")]
                             for x in m["candidates"]],
                       "u": r["url"], "keys": [pkey(x["name"]) for x in m["candidates"]]})
    for r in store.iter_query("kind='ballot_question' AND meta LIKE '%\"has_results\": true%'"):
        m = r["meta"]
        jid = r.get("target") or "state"
        date = m.get("date") or "0000-00-00"
        g = groups.setdefault((jid, date), {"eid": f"x{jid}-{date}", "date": date, "year": date[:4], "target": jid,
                                            "title": f"{jurisdictions.get(jid, {}).get('name', jid)} election {date}",
                                            "kind": "municipal", "official": bool(m.get("official")), "u": r["url"], "c": [], "_seen": set()})
        sig = ("q", m["question"].lower()[:120])
        if sig in g["_seen"]:
            continue
        g["_seen"].add(sig)
        j = jurisdictions.get(jid)
        county = (j.get("counties") or [j["name"].removesuffix(" County")])[0] if j else ""
        title = m["type"] if (len(m["question"]) > 80 or re.match(r"^shall\b", m["question"], re.I)) else m["question"]
        g["c"].append({"i": len(g["c"]), "lvl": "local", "sub": "question", "key": f"{county}|{title}", "t": title, "p": "", "j": county,
                       "vf": 1, "tot": m.get("total", 0), "o": [["Yes", "", m.get("yes", 0), 0, ""], ["No", "", m.get("no", 0), 0, ""]],
                       "u": r["url"], "res": m.get("result"), "q": m["question"][:600], "type": m["type"]})
    out = []
    for g in groups.values():
        g.pop("_seen")
        out.append(g)
    return sorted(out, key=lambda e: (e["date"], e["target"]))


def export_qualifying(store) -> list[dict]:
    return [{"target": r.get("target"), "date": r["meta"].get("date"), "u": r["url"], "contests": r["meta"]["contests"]}
            for r in store.iter_query("kind='qualifying'")]
