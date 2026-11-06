"""Local ballot questions: the question text, from sample ballots, election notices, resolutions
and ordinances calling a referendum (results come from the elections deriver, which turns a
Yes/No contest on a results page into a ballot_question with counts).

A ballot question record (kind ballot_question) carries
  question   the text as printed ("Shall the City of ... be authorized to levy ...?")
  type       Bond / Sales tax / Lodging or excise tax / Homestead exemption / Property tax or levy /
             Debt or revenue limit / Alcohol / Charter amendment / Annexation or incorporation /
             Term or government structure / Recall / Constitutional amendment / Referendum, plus the
             state-specific kinds a project lists in settings.yaml civic.ballot_types
  date       the election date the page gives, when it gives one
  amount / years / purposes  dollar figures, the number of years and the listed projects a
             tax question names, when present
Text-only records and results records for the same jurisdiction, date and type are paired
in the export (ballot_questions.json), each keeping its own source.
"""
from __future__ import annotations

import re

from ..derive import Deriver, register
from .elections import find_date, looks_like_results, question_type
from ..store import Record

QUESTION_RX = re.compile(r"(Shall\s+(?:the\s+)?[^?]{20,900}\?)", re.I | re.S)
HINT = re.compile(r"\b(shall\s+the|shall\s+[a-z .'-]{3,60}\s+taxes\s+be\s+increased|referendum|ballot\s+(?:question|issue|measure)|"
                  r"special\s+election\s+question|sample\s+ballot|homestead\s+exemption|mill\s+levy|bond\s+(?:issue|question)|"
                  r"proposition\s+\w+|be\s+authorized\s+to)\b", re.I)
AMOUNT_RX = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:million|billion))", re.I)
YEARS_RX = re.compile(r"\b(?:for\s+a\s+period\s+of|period\s+of|not\s+to\s+exceed|for)\s+(?:(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+)(?:\(\d+\)\s+)?(?:years?|quarters?|calendar\s+quarters)", re.I)
PLACE_RX = re.compile(r"\b(?:city|town)\s+of\s+((?-i:[A-Z])[\w .'-]+?)(?=\s+(?:be|levy|issue|impose|to|is|and|,|shall)|,|\?|$)|"
                      r"\b((?-i:(?:[A-Z][\w'’-]+\s+){1,3}))County\b", re.I)
PURPOSE_RX = re.compile(r"\bfor\s+(?:the\s+)?(?:purposes?\s+of|following\s+purposes?)\s*[:,]?\s*([^?]{10,400})", re.I)


def parse_questions(text: str) -> list[dict]:
    out, seen = [], set()
    flat = re.sub(r"\s+", " ", text or "")
    for m in QUESTION_RX.finditer(flat):
        q = m.group(1).strip()
        k = re.sub(r"[^a-z]", "", q.lower())[:200]
        if k in seen:
            continue
        seen.add(k)
        item = {"question": q, "type": question_type(q)}
        am = AMOUNT_RX.findall(q)
        if am:
            def val(a):
                n = float(re.sub(r"[^\d.]", "", re.sub(r"\s*(million|billion)", "", a, flags=re.I)) or 0)
                return n * (1e6 if "million" in a.lower() else 1e9 if "billion" in a.lower() else 1)
            item["amount"] = max(am, key=val)
        ym = YEARS_RX.search(q)
        if ym:
            item["years"] = ym.group(1)
        pm = PLACE_RX.search(q)
        if pm:
            item["place"] = (pm.group(1) or (pm.group(2).strip() + " County")).strip()
        pu = PURPOSE_RX.search(q)
        if pu:
            item["purposes"] = pu.group(1).strip()[:400]
        out.append(item)
    return out


def _derive(src: dict, ctx: dict):
    text, title, url = src.get("text") or "", src.get("title") or "", src.get("url") or ""
    if not HINT.search(f"{title} {text[:5000]}") or looks_like_results(title, url, text):
        return  # results pages are the elections deriver's (their Yes/No contests become questions with counts)
    qs = parse_questions(text)
    if not qs:
        return
    date = find_date(text, title)
    for i, q in enumerate(qs[:40]):
        yield Record(src["source_id"], "ballot_question", url, title=f"{q['type']}: {q['question'][:140]}",
                     key=f"{src['source_id']}|ballot_question|{url}|text|{i}", text=q["question"], published=date, channel="derived",
                     target=src.get("target") or "",
                     meta={**q, "date": date, "page_title": title[:200], "has_results": False, "opened": True, "opened_url": url,
                           "doc_type": (src.get("meta") or {}).get("doc_type", "")})


register(Deriver("ballot", "kind IN ('page','document','ordinance','matter','agenda_item') AND source_id NOT LIKE 'subj:%' AND "
                           "(lower(text) LIKE '%shall the%' OR lower(title) LIKE '%referendum%' OR lower(title) LIKE '%ballot%')", _derive))


def export_questions(store, jurisdictions: dict) -> list[dict]:
    """Every ballot question, results paired with text by jurisdiction + date + type."""
    texts, results = [], []
    for r in store.iter_query("kind='ballot_question'"):
        (results if r["meta"].get("has_results") else texts).append(r)
    out = []
    used = set()
    for r in results:
        m = r["meta"]
        match = next((t for t in texts if t.get("target") == r.get("target") and t["meta"].get("type") == m.get("type")
                      and (not t["meta"].get("date") or not m.get("date") or t["meta"]["date"] == m["date"])), None)
        if match:
            used.add(match["id"])
        jid = r.get("target") or ""
        out.append({"target": jid, "name": jurisdictions.get(jid, {}).get("name", jid), "date": m.get("date"), "type": m.get("type"),
                    "question": (match or r)["meta"].get("question") or m.get("question"), "yes": m.get("yes"), "no": m.get("no"),
                    "total": m.get("total"), "result": m.get("result"), "official": m.get("official"),
                    "sources": [r["url"]] + ([match["url"]] if match else []),
                    "amount": (match or {}).get("meta", {}).get("amount"), "years": (match or {}).get("meta", {}).get("years"),
                    "purposes": (match or {}).get("meta", {}).get("purposes")})
    for t in texts:
        if t["id"] in used:
            continue
        m = t["meta"]
        jid = t.get("target") or ""
        out.append({"target": jid, "name": jurisdictions.get(jid, {}).get("name", jid), "date": m.get("date"), "type": m.get("type"),
                    "question": m.get("question"), "yes": None, "no": None, "total": None, "result": None, "official": None,
                    "sources": [t["url"]], "amount": m.get("amount"), "years": m.get("years"), "purposes": m.get("purposes")})
    return sorted(out, key=lambda q: (q["date"] or "", q["target"]))
