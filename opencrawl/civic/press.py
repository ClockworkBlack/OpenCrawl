"""Press releases -> appointments (and who announced them).

Official newsrooms (config/press.yaml: the Governor, Lt. Governor, Attorney General, Secretary of
State, the legislature, state agencies) and every jurisdiction's own news pages are crawled
as kind press_release. This deriver reads those (and news-flash feed items) for appointment
language and writes one `appointment` record per person named:

  "Governor Dale P. Corwin today announced the appointment of Jamie Q. Sample to the State Board
   of Education"                       -> person Jamie Q. Sample, body State Board of Education, by Corwin
  "... appointed John Doe as judge of the District Court of the Fourth Judicial District"
  "The Council appointed Robin Ashby to the Planning Commission (term ending 2028)"
  "appointments to the Board of Natural Resources: Jane Doe of Springfield; John Roe of Riverside"

Everything is quoted from the release that was opened; nothing is inferred. A release that
names several appointees yields several records, each with the sentence it came from.
"""
from __future__ import annotations

import re

from ..derive import Deriver, register
from ..research.stances import split_sentences
from ..store import Record

NAME = r"(?-i:[A-Z][A-Za-z'’\-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'’\-]+|de|van|von|Mc[A-Z][a-z]+)){1,4}(?:,?\s+(?:Jr|Sr|II|III|IV)\.?)?)"
BODY = (r"(?P<body>(?:the\s+)?[A-Z][\w&'’.\-, ]{3,110}?(?:Board of (?:Education|Regents|Elections(?: and Registration)?|Directors|"
        r"Natural Resources|Commissioners|Trustees|Health|Tax Assessors|Equalization|Appeals|Zoning Appeals|Adjustment)|"
        r"Judicial Circuit|Advisory Committee|Planning Commission|Zoning Board|Housing Authority|Development Authority|"
        r"Hospital Authority|Water Authority|Library Board|Ethics Board|Task Force|Board|Commission|Authority|Council|Court|"
        r"Committee|Agency|Department|Office|Corporation|Foundation|Institute|Bureau|Panel|Circuit|System|Trust|Fund|Service|"
        r"Association|University|College|Center|Resources))")
ROLE = r"(?P<role>(?:as\s+(?:a\s+|an\s+|the\s+)?)?(?:chair(?:man|woman|person)?|vice[- ]chair|judge|justice|director|executive director|commissioner|member|trustee|secretary|administrator|superintendent|chief|sheriff|solicitor|district attorney|magistrate|interim [a-z ]+|acting [a-z ]+)?)"
APPOINT_RX = [
    # announced the appointment(s) of NAME to BODY / NAME as ROLE of BODY
    re.compile(r"\b(?:announced|announces|announcing)\s+(?:today\s+)?(?:the\s+)?(?:re-?)?appointments?\s+of\s+(?P<name>" + NAME + r")\s+"
               r"(?:(?P<role1>as\s+[^,.;]{3,60}?)\s+)?(?:to|on|of)\s+" + BODY, re.I),
    # appointed/named/reappointed NAME (as ROLE)? to BODY  |  NAME to serve as ROLE of BODY
    re.compile(r"\b(?:has\s+|have\s+|today\s+)?(?:re-?)?(?:appointed|appoints|named|names|nominated|nominates|selected|selects|tapped)\s+"
               r"(?P<name>" + NAME + r")\s+(?:(?P<role1>as\s+[^,.;]{3,60}?)\s+)?(?:to(?:\s+serve\s+(?:as\s+[^,.;]{3,60}?)?)?|on|of)\s+(?:the\s+)?" + BODY, re.I),
    # NAME has been / was appointed (as ROLE)? to BODY
    re.compile(r"\b(?P<name>" + NAME + r")\s+(?:has\s+been|was|were|is|will\s+be)\s+(?:re-?)?(?:appointed|named|nominated|selected|sworn\s+in)\s+"
               r"(?:(?P<role1>as\s+[^,.;]{3,60}?)\s+)?(?:to|on|of)\s+(?:the\s+)?" + BODY, re.I),
]
LIST_RX = re.compile(r"\b(?:re-?)?appointments?\s+(?:to|on)\s+(?:the\s+)?(?P<body>[A-Z][\w&'’.\-, ]{3,110}?)\s*[:–]\s*(?P<list>[^\n]{10,1500})", re.I)
BY_RX = re.compile(r"\b(?:Governor|Gov\.|Lt\.?\s+Governor|Lieutenant Governor|Attorney General|Secretary of State|Speaker|Mayor|"
                   r"Chair(?:man|woman)?|Commission Chair|Superintendent|President)\s+(?P<by>" + NAME + r")", re.I)
TERM_RX = re.compile(r"\bterms?\s+(?:expiring|ending|to\s+expire|through|that\s+ends)\s+(?:on\s+|in\s+)?(?P<term>[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4})", re.I)
HINT = re.compile(r"\b(appoint|reappoint|names?\b|nominat|sworn in|selected to serve)", re.I)
SKIP_NAME = re.compile(r"\b(Governor|Lieutenant|Attorney General|Secretary|Speaker|Mayor|Senate|House|Board|Commission|Council|"
                       r"State|County|City|Office|Department|Committee|Press|Release|Monday|Tuesday|Wednesday|Thursday|Friday)\b")


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" ,.;:–-")
    return re.sub(r"^(?:the|a|an)\s+", "", s, flags=re.I)


def parse_appointments(text: str, default_by: str = "") -> list[dict]:
    out, seen = [], set()
    by = ""
    m = BY_RX.search(text[:1500])
    if m:
        by = m.group("by")
    by = by or default_by
    sents = split_sentences(text)
    for s in sents:
        if not HINT.search(s) or len(s) > 900:
            continue
        found = []
        for rx in APPOINT_RX:
            for mm in rx.finditer(s):
                name = _clean(mm.group("name"))
                if SKIP_NAME.search(name) or len(name.split()) < 2:
                    continue
                role = _clean((mm.groupdict().get("role1") or "").removeprefix("as ").removeprefix("As "))
                found.append({"person": name, "body": _clean(mm.group("body")), "role": role})
        lm = LIST_RX.search(s)
        if lm:
            body = _clean(lm.group("body"))
            for part in re.split(r"\s*;\s*|\s*,\s*(?=[A-Z][a-z]+\s+[A-Z])|\s+and\s+(?=[A-Z][a-z]+\s+[A-Z])", lm.group("list")):
                nm = re.match(r"\s*(" + NAME + r")", re.sub(r"^\s*(?:and|&)\s+", "", part))
                if nm and not SKIP_NAME.search(nm.group(1)):
                    rest = re.sub(r"^\s*(?:and|&)\s+", "", part)[nm.end():]
                    found.append({"person": _clean(nm.group(1)), "body": body, "role": "", "of": _clean(rest)[:80]})
        tm = TERM_RX.search(s)
        for f in found:
            key = (f["person"].lower(), f["body"].lower())
            if key in seen:
                continue
            seen.add(key)
            f.update({"by": by, "term": tm.group("term") if tm else "", "text": s[:400]})
            out.append(f)
    return out


def _derive(src: dict, ctx: dict):
    text = src.get("text") or ""
    title = src.get("title") or ""
    if not HINT.search(f"{title} {text[:3000]}"):
        return
    level = "state" if (src.get("meta") or {}).get("level") == "state" or src["source_id"].startswith("press:") else (src.get("target") or "")
    for i, a in enumerate(parse_appointments(f"{title}. {text}")):
        yield Record(src["source_id"], "appointment", src["url"], title=f"{a['person']} appointed to {a['body']}", text=a["text"],
                     published=src.get("published"), channel="derived", target=src.get("target") or "",
                     key=f"{src['source_id']}|appointment|{src['url']}|{a['person'].lower()}|{a['body'].lower()}",
                     meta={"person": a["person"], "body": a["body"], "role": a["role"], "by": a["by"], "term": a["term"],
                           "of": a.get("of", ""), "level": level, "release_title": title[:200], "opened": True, "opened_url": src["url"]})


register(Deriver("press", "kind IN ('press_release','page','feed_item') AND source_id NOT LIKE 'subj:%' AND "
                          "(lower(title) LIKE '%appoint%' OR lower(text) LIKE '%appoint%' OR lower(title) LIKE '%names %')", _derive))
