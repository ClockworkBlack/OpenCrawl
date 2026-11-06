"""A subject's public career as a sourced timeline.

Entries come only from records that state them:
  election     every contest listed on the subject (`races`: {election_id, date, office, party, votes, pct, outcome,
               level, url, title, county, type}, e.g. from `opencrawl subjects import`)
  office       Legistar office records (body, title, start/end), officials listings,
               legislature member records (chamber, district, sessions)
  appointment  appointment announcements (press releases)
  attendance   listed as a member present in council minutes (first and last date seen,
               per body) — a floor on when they served, never an exact term
  bio          the candidate's own biography: "served as ... from 2010 to 2014",
               "elected in 2018", "appointed to the ... in 2020" (basis: own bio)
  membership   affiliations found by opencrawl.stances (caucuses, boards, associations)

Each entry: {date (start or date), end_date, kind, role, body, target, how, party, outcome,
text, refs:[i], basis, evidence}. Entries are sorted by date; nothing is interpolated.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..names import name_regex, name_variants
from ..store import Store

BIO_SERVED = re.compile(
    r"\b(?:served|serving|has served|is serving|worked)\s+(?:as\s+(?:the\s+|a\s+|an\s+)?(?P<role>[A-Za-z][\w'’\- ]{2,60}?)\s+)?"
    r"(?:on|of|in|for|with)\s+(?:the\s+)?(?P<body>[A-Z][\w&'’.\- ]{3,80}?)(?:\s+(?:from|since|in)\s+(?P<y1>(?:19|20)\d\d)"
    r"(?:\s*(?:to|-|–|until|through)\s*(?P<y2>(?:19|20)\d\d|present|today))?)?(?=[,.;:!)\n]|\s+and\s+|\s+where\s+|$)")
BIO_ELECTED = re.compile(r"\b(?:was\s+|were\s+|first\s+)?(?:elected|re-?elected|appointed|sworn in)\s+(?:to|as)\s+(?:the\s+|a\s+|an\s+)?"
                         r"(?P<body>[A-Z][\w&'’.\- ]{3,80}?)(?:\s+in\s+(?P<y1>(?:19|20)\d\d))?(?=[,.;:!)\n]|\s+and\s+|\s+where\s+|\s+by\s+|$)")
BIO_ELECTED2 = re.compile(r"\b(?:first\s+)?(?:elected|re-?elected|appointed|sworn in)\s+in\s+(?P<y1>(?:19|20)\d\d)\b")
ROLE_WORDS = re.compile(r"\b(mayor|council\s*(?:member|man|woman)|commissioner|chair(?:man|woman)?|representative|senator|"
                        r"board member|trustee|clerk|sheriff|judge|solicitor|district attorney|alderman|president|director)\b", re.I)


def _d10(r: dict) -> str:
    return (r.get("published") or r.get("fetched_at") or "")[:10]


class CareerIndex:
    """Lookups shared by every candidate in one profiles run: office records and minutes attendance per target
    (read once, on first use) and appointments by surname, instead of a filtered scan per candidate."""

    def __init__(self, store: Store):
        self.store = store
        self._officials: dict[str, dict[str, list[dict]]] = {}
        self._attendance: dict[str, dict[str, list[tuple]]] = {}
        self._appointments: dict[str, list[dict]] | None = None

    @staticmethod
    def _last(name: str) -> str:
        toks = re.findall(r"[a-z]+", (name or "").lower())
        toks = [t for t in toks if t not in ("jr", "sr", "ii", "iii", "iv")]
        return toks[-1] if toks else ""

    def officials(self, jid: str, last: str) -> list[dict]:
        if jid not in self._officials:
            idx: dict[str, list[dict]] = {}
            for r in self.store.iter_query("target=? AND kind='official'", (jid,)):
                idx.setdefault(self._last(r.get("title") or ""), []).append(r)
            self._officials[jid] = idx
        return self._officials[jid].get(last, [])

    def attendance(self, jid: str, last: str) -> list[tuple]:
        """[(present name, body, date, url)] for minutes-derived items in jid listing someone with this surname."""
        if jid not in self._attendance:
            idx: dict[str, list[tuple]] = {}
            for r in self.store.iter_query("target=? AND kind='agenda_item' AND channel='derived'", (jid,)):
                m = r["meta"]
                d = _d10(r)
                if m.get("from") != "minutes" or not d:
                    continue
                body = m.get("EventBodyName") or "council"
                for p in m.get("present") or []:
                    idx.setdefault(self._last(p), []).append((p, body, d, r["url"]))
            self._attendance[jid] = idx
        return self._attendance[jid].get(last, [])

    def appointments(self, last: str) -> list[dict]:
        if self._appointments is None:
            idx: dict[str, list[dict]] = {}
            for r in self.store.iter_query("kind='appointment'"):
                idx.setdefault(self._last((r.get("meta") or {}).get("person") or ""), []).append(r)
            self._appointments = idx
        return self._appointments.get(last, [])


def career_for(cand: dict, store: Store, sources, affiliations: list[dict] | None = None,
               legis_member: dict | None = None, index: CareerIndex | None = None) -> list[dict]:
    """sources: a stances.SourceList (shared with the profile) so refs indexes line up.
    index: a CareerIndex shared across candidates (built on the fly when not given)."""
    index = index or CareerIndex(store)
    out: list[dict] = []
    variants = cand.get("variants") or name_variants(cand["name"], cand.get("aliases", []))
    rx = name_regex(variants)
    last = (variants[0].split()[-1] if variants else cand["name"].split()[-1]).lower()
    jids = [j for j in cand.get("targets", []) if j]

    # 1. elections listed on the subject
    for r in cand.get("races") or []:
        rec = {"url": r.get("url"), "title": r.get("title") or f"Official results: {r.get('office')}, {r.get('date')}", "published": r.get("date"),
               "kind": "election_result", "evidence": "primary"}
        i = sources.add(rec) if r.get("url") else None
        primary = (r.get("type") or "").lower() == "primary" or "primary" in (r.get("office") or "").lower()
        out.append({"date": r.get("date"), "end_date": "", "kind": "election", "role": "candidate", "body": r.get("office"),
                    "target": r.get("county") or "", "level": r.get("level"), "party": r.get("party"),
                    "outcome": r.get("outcome") or "did not win", "votes": r.get("votes"), "pct": r.get("pct"),
                    "how": ("primary" if primary else "general/special") + " election",
                    "text": f"{r.get('office')}: {r.get('outcome') or 'did not win'} ({r.get('votes')} votes, {r.get('pct')}%)",
                    "refs": [i] if i is not None else [], "basis": "official results", "evidence": "primary"})
    # 2. office records (Legistar / any `official` record naming them)
    for r in [r for jid in jids for r in index.officials(jid, last)][:200]:
        if not rx.search(r.get("title") or ""):
            continue
        m = r.get("meta", {})
        i = sources.add(r)
        out.append({"date": (m.get("OfficeRecordStartDate") or m.get("start") or "")[:10], "end_date": (m.get("OfficeRecordEndDate") or m.get("end") or "")[:10],
                    "kind": "office", "role": m.get("OfficeRecordTitle") or m.get("role") or m.get("title") or "member",
                    "body": m.get("OfficeRecordBodyName") or m.get("body") or "", "target": r.get("target") or "",
                    "how": "office record", "text": r.get("text") or r.get("title") or "", "refs": [i],
                    "basis": "office record (" + (m.get("provider") or r.get("channel") or "record") + ")", "evidence": "primary"})
    if legis_member:
        for t in legis_member.get("terms") or []:
            out.append({"date": t.get("from", ""), "end_date": t.get("to", ""), "kind": "office", "role": t.get("role") or "member",
                        "body": t.get("body") or "state legislature", "target": t.get("target") or "", "how": "legislature member record",
                        "text": t.get("text") or "", "refs": [sources.add({"url": t.get("url"), "title": t.get("title") or "legislature member record"})] if t.get("url") else [],
                        "basis": "legislature member record", "evidence": "primary"})
    # 3. appointments
    for r in index.appointments(last)[:100]:
        m = r.get("meta", {})
        if not rx.search(m.get("person") or ""):
            continue
        i = sources.add(r)
        out.append({"date": _d10(r), "end_date": "", "kind": "appointment", "role": m.get("role") or "appointee", "body": m.get("body") or "",
                    "target": m.get("level") or "state", "how": f"appointed by {m.get('by')}" if m.get("by") else "appointed",
                    "text": (r.get("text") or "")[:300], "refs": [i], "basis": "appointment announcement", "evidence": r.get("evidence") or "primary"})
    # 4. attendance in minutes (floor on service dates, per body); counted per meeting (minutes document)
    if jids:
        seen: dict[tuple, dict] = {}
        for jid in jids:
            for p, body, d, url in index.attendance(jid, last):
                if not (rx.search(p) or p.lower().endswith(last)):
                    continue
                e = seen.setdefault((jid, body), {"first": d, "last": d, "docs": set(), "urls": set()})
                e["first"], e["last"] = min(e["first"], d), max(e["last"], d)
                e["docs"].add(url)
                if len(e["urls"]) < 6:
                    e["urls"].add(url)
        for e in seen.values():
            e["n"] = len(e["docs"])
        for (jid, body), e in seen.items():
            idx = [sources.add({"url": u, "title": "Council minutes", "published": ""}) for u in sorted(e["urls"])]
            out.append({"date": e["first"], "end_date": e["last"], "kind": "attendance", "role": "member present", "body": body, "target": jid,
                        "how": f"listed present in minutes ({e['n']} meetings)", "text": f"Listed as present at {e['n']} meetings between {e['first']} and {e['last']}",
                        "refs": idx, "basis": "minutes attendance (a floor on service, not a term)", "evidence": "primary"})
    # 5. own biography
    for r in store.read(f"subj:{cand['id']}", "statement"):
        text = r.get("text") or ""
        if not re.search(r"\b(about|bio|meet|background|experience|served|elected|appointed)\b", f"{r.get('title') or ''} {text[:400]}", re.I):
            continue
        for s in re.split(r"(?<=[.!?])\s+", text):
            if len(s) > 400:
                continue
            for m in BIO_SERVED.finditer(s):
                out.append({"date": m.group("y1") or "", "end_date": (m.group("y2") or "").replace("present", "").replace("today", ""),
                            "kind": "bio", "role": (m.group("role") or "").strip(), "body": m.group("body").strip(), "target": "",
                            "how": "served (own bio)", "text": s[:300], "refs": [sources.add(r)], "basis": "subject's own biography", "evidence": "primary"})
            for m in BIO_ELECTED.finditer(s):
                how = "elected" if re.search(r"elected", m.group(0), re.I) else "appointed"
                y = m.group("y1") or (BIO_ELECTED2.search(s).group("y1") if BIO_ELECTED2.search(s) else "")
                out.append({"date": y, "end_date": "", "kind": "bio", "role": "", "body": m.group("body").strip(), "target": "", "how": how + " (own bio)",
                            "text": s[:300], "refs": [sources.add(r)], "basis": "subject's own biography", "evidence": "primary"})
    # 6. memberships (from stances)
    for a in affiliations or []:
        out.append({"date": a.get("date", ""), "end_date": "", "kind": "membership", "role": a.get("role", ""), "body": a.get("org", ""), "target": "",
                    "how": a.get("basis", ""), "text": a.get("text", ""), "refs": a.get("refs", []), "basis": a.get("basis", ""), "evidence": a.get("evidence", "primary")})
    # dedupe and order
    seen_k, uniq = set(), []
    for e in out:
        k = (e["kind"], (e.get("body") or "").lower(), e.get("date") or "", (e.get("role") or "").lower(), e.get("outcome") or "")
        if k in seen_k:
            continue
        seen_k.add(k)
        uniq.append(e)
    uniq.sort(key=lambda e: (e.get("date") or "9999", e["kind"]))
    return uniq


def summary(career: list[dict]) -> str:
    """One line: offices won and held, from the timeline (facts only)."""
    wins = [e for e in career if e["kind"] == "election" and e.get("outcome") in ("won", "elected", "unopposed") and "primary" not in e["how"]]
    held = [e for e in career if e["kind"] in ("office",)]
    parts = []
    if wins:
        parts.append("won " + "; ".join(f"{e['body']} ({e['date'][:4]})" for e in wins[-6:]))
    if held:
        parts.append("office records: " + "; ".join(f"{e['role']}, {e['body']} ({e['date'][:4] or '?'}–{e['end_date'][:4] or 'present'})" for e in held[:6]))
    losses = [e for e in career if e["kind"] == "election" and e.get("outcome") not in ("won", "elected", "unopposed") and "primary" not in e["how"]]
    if losses:
        parts.append("ran and did not win: " + "; ".join(f"{e['body']} ({e['date'][:4]})" for e in losses[-4:]))
    return "; ".join(parts)
