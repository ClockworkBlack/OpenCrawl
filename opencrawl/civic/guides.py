"""Endorsements, scorecards and voter guides -> per-candidate support records.

Pages crawled from config/guides.yaml (kind guide, source ids guide:*) are read for
  endorsements    "NAME — Office", "NAME (Office)", "endorses NAME for Office", lists and tables
  ratings         "NAME ... A+ / 100% / 87" on scorecard pages
  questionnaires  "Question ... / NAME: answer" blocks (Vote411-style guides)
Each hit that resolves to a known subject (config/subjects.yaml and imported subjects, by name
variants) is written under cand:<id> as
  connection  type endorsement / rating (party = the organization, basis "endorser's own page")
  guide_answer                       (meta.question, meta.guide)
and every hit, resolved or not, as an `endorsement` / `rating` record on the guide source, so
unmatched names can be reviewed. Evidence is primary: the organization's own statement, cited
to the page that was opened.
"""
from __future__ import annotations

import re

from ..derive import Deriver, register
from ..names import name_variants
from ..store import Record

OFFICE_WORDS = (r"(?:governor|lieutenant governor|lt\.? governor|attorney general|secretary of state|state school superintendent|"
                r"commissioner of (?:agriculture|insurance|labor)|public service commission(?:er)?|psc|u\.?s\.? (?:senate|house|representative|congress)|"
                r"congress(?:ional)?|state (?:senate|house|representative|senator)|senate|house|district attorney|solicitor|sheriff|judge|"
                r"mayor|city council|council|commission(?:er)?|school board|board of education|clerk|tax commissioner|coroner|"
                r"probate|magistrate|superior court|supreme court|court of appeals)(?:\s+(?:district|dist\.?|seat|post|ward)\s*\d+[A-Za-z]?)?")
NAME = r"(?-i:[A-Z][A-Za-z'’.\-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’.\-]+|\"[^\"]{2,20}\"|“[^”]{2,20}”)){1,4}(?:,?\s+(?:Jr|Sr|II|III|IV)\.?)?)"
ENDORSE_LINE = re.compile(r"^\s*(?:[-*•·]\s*)?(?P<name>" + NAME + r")\s*(?:[-–—:|(,]\s*|\s+for\s+|\s+–\s+)(?P<office>[^|\n()]{3,80}?)\)?\s*(?:\||$)", re.I | re.M)
ENDORSES = re.compile(r"\b(?:endorse[sd]?|is proud to endorse|announces? (?:its|our|the) endorsement of|supports?)\s+(?P<name>" + NAME + r")"
                      r"(?:\s+(?:for|in the race for|in)\s+(?P<office>[^.,;\n]{3,80}))?", re.I)
RATING_ROW = re.compile(r"^\s*(?:[-*•·]\s*)?(?P<name>" + NAME + r")\s*(?:\(\s*(?P<party>[DRLI])\s*[-–]?\s*[^)]*\))?\s*(?:[|:–—-]\s*)?(?P<mid>[^|\n]{0,60}?)\s*[|:]?\s*"
                        r"(?P<rating>[A-F][+\-]?|\d{1,3}\s?%|\d{1,3}(?:\.\d)?(?=\s*(?:%|/100|$|\|)))\s*(?:%|/100)?\s*(?:\||$)", re.I | re.M)
QUESTION = re.compile(r"^\s*(?:Q(?:uestion)?\s*\d*[:.)]\s*|\d{1,2}[.)]\s+)?(?P<q>[^\n?]{15,300}\?)\s*$", re.M)
ANSWER = re.compile(r"^\s*(?P<name>" + NAME + r")\s*(?:\([^)]{1,40}\))?\s*[:：]\s*(?P<a>.{20,2500}?)(?=\n\s*(?:" + NAME + r")\s*(?:\([^)]{1,40}\))?\s*[:：]|\n\s*\n|\Z)", re.S | re.M)
NOISE = re.compile(r"\b(click|read more|donate|subscribe|share|menu|privacy|cookie|login|sign up|search|copyright)\b", re.I)
OFFICE_RX = re.compile(OFFICE_WORDS, re.I)


def _candidates(ctx: dict) -> dict:
    """{normalized variant: candidate} built once per run."""
    if "_guide_cands" in ctx:
        return ctx["_guide_cands"]
    from ..research.subjects import load_subjects

    out = {}
    root, cfg = ctx.get("root"), ctx.get("cfg") or {}
    if root:
        for c in load_subjects(root, cfg):
            for v in c.get("variants") or name_variants(c["name"], c.get("aliases", [])):
                out.setdefault(re.sub(r"[^a-z]", "", v.lower()), c)
    ctx["_guide_cands"] = out
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def parse_endorsements(text: str) -> list[dict]:
    out, seen = [], set()
    for m in ENDORSES.finditer(text):
        name, office = m.group("name").strip(), (m.group("office") or "").strip()
        if _norm(name) not in seen and not NOISE.search(name):
            seen.add(_norm(name))
            out.append({"name": name, "office": office[:80], "line": m.group(0)[:200]})
    for m in ENDORSE_LINE.finditer(text):
        name, office = m.group("name").strip(), m.group("office").strip()
        if NOISE.search(name) or NOISE.search(office) or not OFFICE_RX.search(office):
            continue
        if _norm(name) in seen:
            continue
        seen.add(_norm(name))
        out.append({"name": name, "office": office[:80], "line": m.group(0).strip()[:200]})
    return out


NAME_CELL = re.compile(r"^" + NAME + r"$")
RATING_CELL = re.compile(r"^(?:[A-F][+\-]?|\d{1,3}(?:\.\d)?\s?%|\d{1,3}(?:\.\d)?(?:/100)?)$")


def _table_ratings(text: str, seen: set) -> list[dict]:
    """Rows of a markdown table whose header names a score/rating/grade column."""
    out, cols = [], None
    for ln in text.splitlines():
        if "|" not in ln:
            cols = None
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cols is None:
            if any(re.search(r"score|rating|grade|%", c, re.I) for c in cells):
                cols = [c.lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rc = next((i for i, c in enumerate(cols) if re.search(r"score|rating|grade|%", c)), None)
        if rc is None or rc >= len(cells) or not RATING_CELL.match(cells[rc]):
            continue
        name = next((c for c in cells if NAME_CELL.match(c) and len(c.split()) >= 2), None)
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        party = next((c.upper() for c in cells if re.fullmatch(r"[DRLI]", c, re.I)), "")
        office = next((c for i, c in enumerate(cells) if i != rc and c != name and re.search(r"district|house|senate|\d", c, re.I)), "")
        out.append({"name": name, "rating": cells[rc].replace(" ", ""), "party": party, "office": office[:60], "line": ln.strip()[:200]})
    return out


def parse_ratings(text: str) -> list[dict]:
    seen: set = set()
    out = _table_ratings(text, seen)
    for m in RATING_ROW.finditer(text):
        name = m.group("name").strip()
        if NOISE.search(name) or _norm(name) in seen or len(name.split()) < 2:
            continue
        rating = m.group("rating").replace(" ", "")
        if re.fullmatch(r"\d{1,3}(?:\.\d)?", rating) and not re.search(r"%|/100|score|rating|grade", text[max(0, m.start() - 300):m.end() + 50], re.I):
            continue  # a bare number next to a name is not a rating unless the page is about scores
        seen.add(_norm(name))
        out.append({"name": name, "rating": rating + ("%" if re.fullmatch(r"\d{1,3}(?:\.\d)?", rating) and "%" in m.group(0) else ""),
                    "party": (m.group("party") or "").upper(), "office": (m.group("mid") or "").strip()[:60], "line": m.group(0).strip()[:200]})
    return out


def parse_questionnaire(text: str) -> list[dict]:
    """[{question, answers: [{name, answer}]}] from guide pages that print a question then each candidate's answer."""
    out = []
    blocks = re.split(r"\n(?=\s*(?:Q(?:uestion)?\s*\d*[:.)]|\d{1,2}[.)]\s+[A-Z]))", text)
    for b in blocks:
        qm = QUESTION.search(b)
        if not qm:
            continue
        q = qm.group("q").strip()
        answers = []
        for am in ANSWER.finditer(b[qm.end():]):
            name, a = am.group("name").strip(), re.sub(r"\s+", " ", am.group("a")).strip()
            if len(name.split()) >= 2 and not NOISE.search(name) and len(a) >= 20:
                answers.append({"name": name, "answer": a[:2500]})
        if answers:
            out.append({"question": q, "answers": answers})
    return out


def _derive(src: dict, ctx: dict):
    text, title, url = src.get("text") or "", src.get("title") or "", src.get("url") or ""
    meta = src.get("meta") or {}
    guide = meta.get("guide_name") or src["source_id"].split(":", 1)[-1]
    gkind = meta.get("guide_kind") or ""
    cands = _candidates(ctx)
    date = src.get("published")
    hay = f"{title}\n{text}"
    if gkind in ("endorsements", "party", "") and re.search(r"\bendors", hay, re.I):
        for i, e in enumerate(parse_endorsements(text)[:400]):
            c = cands.get(_norm(e["name"]))
            yield Record(src["source_id"], "endorsement", url, title=f"{guide} endorses {e['name']}" + (f" ({e['office']})" if e["office"] else ""),
                         text=e["line"], published=date, channel="derived", key=f"{src['source_id']}|endorsement|{url}|{_norm(e['name'])}",
                         meta={"org": guide, "name": e["name"], "office": e["office"], "subject_id": c["id"] if c else None,
                               "matched": bool(c), "opened": True, "opened_url": url})
            if c:
                yield Record(f"subj:{c['id']}", "connection", url, title=f"Endorsement: {guide}", text=e["line"], published=date,
                             channel="derived", evidence="primary", target=c.get("target", ""),
                             key=f"subj:{c['id']}|connection|endorsement|{_norm(guide)}|{url}",
                             meta={"type": "endorsement", "party": guide, "office": e["office"], "direction": "received",
                                   "basis": "endorser's own page", "subject_id": c["id"],
                                   "opened": True, "opened_url": url})
    if gkind in ("scorecard", "") and re.search(r"scorecard|rating|grade|score", hay, re.I):
        for r in parse_ratings(text)[:400]:
            c = cands.get(_norm(r["name"]))
            yield Record(src["source_id"], "rating", url, title=f"{guide}: {r['name']} {r['rating']}", text=r["line"], published=date,
                         channel="derived", key=f"{src['source_id']}|rating|{url}|{_norm(r['name'])}",
                         meta={"org": guide, "name": r["name"], "rating": r["rating"], "party": r["party"], "office": r["office"],
                               "subject_id": c["id"] if c else None, "matched": bool(c), "opened": True, "opened_url": url})
            if c:
                yield Record(f"subj:{c['id']}", "connection", url, title=f"Rating: {guide} {r['rating']}", text=r["line"], published=date,
                             channel="derived", evidence="primary", target=c.get("target", ""),
                             key=f"subj:{c['id']}|connection|rating|{_norm(guide)}|{url}",
                             meta={"type": "rating", "party": guide, "rating": r["rating"], "year": (date or "")[:4],
                                   "basis": "scorecard", "subject_id": c["id"], "opened": True, "opened_url": url})
    if gkind in ("questionnaire", "") and "?" in text:
        for q in parse_questionnaire(text)[:60]:
            for a in q["answers"]:
                c = cands.get(_norm(a["name"]))
                if not c:
                    continue
                yield Record(f"subj:{c['id']}", "guide_answer", url, title=f"{guide}: {q['question'][:120]}", text=a["answer"], published=date,
                             channel="derived", evidence="primary", target=c.get("target", ""),
                             key=f"subj:{c['id']}|guide_answer|{url}|{_norm(q['question'])[:80]}",
                             meta={"question": q["question"], "guide": guide, "subject_id": c["id"],
                                   "opened": True, "opened_url": url})


register(Deriver("guides", "kind='guide' AND source_id LIKE 'guide:%'", _derive))
