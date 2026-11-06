"""Subject history, ideology, stances and support, as sourced profile items.

A downstream site can only reason about "what they may support next" from sourced items: stated
positions, statements, ratings, endorsements, official votes and sponsored bills (it
needs at least two). This module turns everything the crawler has on a candidate into
exactly those items, each with the record it came from, and grades each by evidence:

  positions     sentences in the candidate's own words (campaign site, verified posts,
                meeting transcripts where the candidate is the speaker, questionnaire
                answers) or reported with attribution (a listed outlet quoting them),
                that take a stance on an issue (config/issues.yaml, config/topics.yaml)
  statements    the candidate's own sentences with a stance cue but no matched issue
  ideology      documented markers only: self-descriptions ("a conservative Republican"),
                caucus/organization memberships, interest-group ratings, endorsements,
                party. Composed into one sourced line; never a guess.
  affiliations  "member of the X Caucus", "serves on the board of Y", "former chair of Z"
  endorsements  received (and given), with who and when
  ratings       "A rating from the State Chamber", "scored 100% with ..."
  votes         council roll calls (Legistar, minutes) and legislature votes
  bills         Legistar sponsorships and legislature bills (role sponsor / co-sponsor)
  ordinances    supported / opposed: votes and sponsorships on ordinances and resolutions

Nothing here is an inference: every item quotes or cites a record that was opened.
Unverified material (anything from a source that is neither the candidate's own
channel, an official record nor a listed outlet) never enters; it stays in the Rumor
Mill. Items parsed from transcripts or scans carry needs_review when speaker
attribution rests on a label the crawler read rather than a byline.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import ROOT
from ..names import name_regex, name_variants
from ..store import Store
from ..topics import tag

STANCE_CUES = re.compile(
    r"\b(support\w*|oppos\w*|against|in favou?r|favou?r\w*|back(?:s|ed|ing)?|believe\w*|will (?:fight|work|push|vote|protect|"
    r"defend|expand|cut|lower|raise|fund|end|stop|ban|repeal|bring|keep|invest|secure|restore|hold|deliver|make|ensure|never|"
    r"always|continue|prioritize|champion|advocate)|would (?:vote|support|oppose|sign|veto|back)|voted? (?:for|against|to|no|yes)|"
    r"committed to|commit\w*|pledge\w*|promis\w*|priorit\w*|plan to|propos\w*|call\w* for|must\b|should\b|need\w* to|"
    r"stand\w* (?:for|with|against|up)|protect\w*|defend\w*|expand\w*|repeal\w*|ban\w*|fight\w*|cosponsor\w*|co-sponsor\w*|"
    r"introduc\w*|sponsor\w*|author\w*|reject\w*|condemn\w*|endorse\w*|urge\w*|demand\w*|cut\b|lower\b|eliminate\w*|"
    r"i am (?:for|against|committed)|i['’]m (?:for|against|committed)|i (?:want|will|won['’]t|refuse|pledge|promise|believe|"
    r"support|oppose|stand|voted|introduced|sponsored|cosponsored|co-sponsored|led|fought|secured|delivered))\b", re.I)
FIRST_PERSON = re.compile(r"\b(i|i['’]m|i['’]ve|i['’]ll|my|we|we['’]re|we['’]ll|our|me)\b", re.I)
ATTRIB = re.compile(r"\b(said|says|told|wrote|posted|argued|added|called|explained|noted|stated|announced|vowed|pledged|"
                    r"promised|insisted|contended|maintained|responded|tweeted|declared|according to)\b", re.I)
NOISE_SENT = re.compile(r"\b(cookie|privacy policy|all rights reserved|click here|subscribe|newsletter|sign up|donate now|"
                        r"paid for by|menu|skip to|read more|share this|javascript|©|copyright)\b", re.I)
SELF_DESC = re.compile(
    r"\b(?:as\s+)?(?:a|an)\s+(?P<label>(?:(?:lifelong|proud|common[- ]sense|constitutional|fiscal|pragmatic|independent[- ]minded|"
    r"pro-?(?:life|choice|business|labor|growth|police|gun))\s+)?(?:conservative|progressive|moderate|liberal|libertarian|"
    r"populist|centrist|independent|democrat|republican|democratic socialist|socialist|constitutionalist)(?:\s+(?:republican|"
    r"democrat|independent))?)\b", re.I)
IDEOLOGY_MARKERS = {
    "conservative": r"\b(conservative|america first|maga|pro-?life|pro-?second amendment|pro-?2a|\b2a\b|limited government|law and order|"
                    r"back the blue|lower taxes|freedom caucus|tea party|constitutional carry|school choice|secure the border|"
                    r"family values|traditional values|god[- ]given rights|anti-?abortion)\b",
    "progressive": r"\b(progressive|democratic socialist|working families|pro-?choice|reproductive freedom|medicare for all|"
                   r"green new deal|living wage|union (?:member|household|strong)|black lives matter|lgbtq\+? rights|"
                   r"expand medicaid|common[- ]sense gun (?:laws|safety|reform)|climate action|racial justice|equity)\b",
    "moderate": r"\b(moderate|centrist|bipartisan|common[- ]sense|pragmatic|across the aisle|problem[- ]solver|"
                r"independent[- ]minded|reach(?:es|ing)? across|work(?:s|ing)? with both parties|middle ground)\b",
    "libertarian": r"\b(libertarian|free market|liberty|end the drug war|personal freedom|small government)\b",
    "populist": r"\b(populist|drain the swamp|anti-?establishment|outsider|career politicians|the people over|special interests)\b",
}
AFFIL = re.compile(
    r"\b(?P<role>member|chair(?:man|woman|person)?|vice[- ]chair|co-?chair|founder|co-?founder|president|vice president|"
    r"treasurer|secretary|director|board member|trustee|delegate|whip|caucus chair|past president|former chair(?:man|woman)?)"
    r"\s+(?:of|on|for)\s+(?:the\s+)?(?P<org>[A-Z][\w&'’.\- ]{3,70}?(?:Caucus|Association|Chamber(?: of Commerce)?|Union|Club|"
    r"Coalition|Committee|Board|Commission|Authority|Foundation|Federation|Society|Alliance|Council|League|Lodge|Rotary|Kiwanis|"
    r"Lions|NAACP|PTA|PTO|Party|Bar|Institute|Network|Fund|Center|Church|Ministries|Guard|Legion|VFW|Task Force|Delegation|"
    r"Group|Team|Board of (?:Directors|Education|Commissioners|Trustees)))\b", re.I)
SERVED = re.compile(r"\b(?:served|serves|serving|sat|sits|sitting)\s+(?:on|as|in)\s+(?:the\s+)?(?P<org>[A-Z][\w&'’.\- ]{3,70}?"
                    r"(?:Caucus|Association|Chamber(?: of Commerce)?|Union|Club|Coalition|Committee|Board|Commission|Authority|"
                    r"Foundation|Council|League|Task Force|Board of (?:Directors|Education|Commissioners|Trustees)))\b", re.I)
ENDORSED_BY = re.compile(r"\b(?:endorsed|backed|supported)\s+by\s+(?:the\s+)?(?P<who>[A-Z][\w&'’.\- ]{2,80}?)(?=[,.;:!)\n]|\s+and\s+|\s+for\s+|\s+in\s+|$)")
ENDORSEMENT_OF = re.compile(r"\b(?:endorsement|support|backing)\s+(?:of|from)\s+(?:the\s+)?(?P<who>[A-Z][\w&'’.\- ]{2,80}?)(?=[,.;:!)\n]|\s+and\s+|\s+for\s+|\s+in\s+|$)")
X_ENDORSES = re.compile(r"(?P<who>(?:The\s+)?[A-Z][\w&'’.\- ]{2,80}?)\s+(?:has\s+|have\s+|today\s+|formally\s+|officially\s+)?"
                        r"endorse[sd]?\s+(?P<whom>[A-Z][\w'’.\- ]{3,60}?)(?=[,.;:!)\n]|\s+for\s+|\s+in\s+|$)")
RATING = re.compile(r"\b(?:rated|received|earned|holds?|has|got|given)\s+(?:an?\s+)?[\"“]?(?P<rating>[A-F][+\-]?|\d{1,3}\s?%|\d{1,3}\s+percent|"
                    r"\d(?:\.\d)?/\d)[\"”]?\s+(?:rating|grade|score|lifetime (?:rating|score))?\s*(?:from|by|with)\s+(?:the\s+)?"
                    r"(?P<org>[A-Z][\w&'’.\- ]{2,70}?)(?=[,.;:!)\n]|\s+in\s+(?:20\d\d)|\s+for\s+|$)", re.I)
RATING2 = re.compile(r"(?P<org>(?:The\s+)?[A-Z][\w&'’.\- ]{2,70}?)\s+(?:gave|gives|rated|graded|scored|awarded)\s+(?:him|her|them|"
                     r"[A-Z][\w'’.\- ]{3,40}?)\s+(?:an?\s+)?[\"“]?(?P<rating>[A-F][+\-]?|\d{1,3}\s?%|\d{1,3}\s+percent)[\"”]?")
YEAR = re.compile(r"\b(20\d\d|19\d\d)\b")
SPEAKER = re.compile(r"^\s*(?:\[[0-9:]+\]\s*)?(?:<v\s+)?(?P<name>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){0,3})\s*[:>]\s*(?P<said>.+)$")


@lru_cache(maxsize=2)
def _issues(path: str) -> tuple[tuple[str, re.Pattern], ...]:
    p = Path(path)
    if not p.exists():
        return ()
    d = yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    return tuple((name, re.compile(rx, re.I)) for name, rx in (d.get("issues") or {}).items())


def issues_of(text: str, root: Path | None = None) -> list[str]:
    hay = (text or "")[:4000]
    out = [name for name, rx in _issues(str((root or ROOT) / "config" / "issues.yaml")) if rx.search(hay)]
    if not out:
        out = tag(hay, path=(root or ROOT) / "config" / "topics.yaml")
    return out


SENT_SPLIT = re.compile(r"(?<=[.!?])(?<![A-Z]\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bGov\.)(?<!\bLt\.)(?<!\bSen\.)"
                        r"(?<!\bRep\.)(?<!\bNo\.)(?<!\bSt\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bvs\.)(?<!\bU\.S\.)\s+(?=[A-Z\"“(])")


def split_sentences(text: str) -> list[str]:
    """Sentences, without breaking after initials ("Jordan P. Hale") or common abbreviations."""
    return [p.strip() for p in SENT_SPLIT.split(re.sub(r"\s+", " ", text or "")) if p.strip()]


@lru_cache(maxsize=1024)
def sentences(text: str) -> tuple[str, ...]:
    """Sentence split, memoized: each record's text is read by several extractors (positions, markers,
    affiliations, endorsements, ratings), and is split once."""
    return tuple(p for p in split_sentences(text) if 25 <= len(p) <= 420)


class TranscriptSpeakers:
    """Speaker-labeled lines with a stance cue in each target's transcripts, indexed by speaker surname,
    built once per target per run (instead of re-reading every transcript for every candidate)."""

    def __init__(self, store: Store):
        self.store = store
        self._idx: dict[str, dict[str, list[tuple]]] = {}

    def lines(self, jid: str, last: str) -> list[tuple]:
        if jid not in self._idx:
            idx: dict[str, list[tuple]] = {}
            for t in self.store.iter_query("target=? AND kind='transcript'", (jid,)):
                meta = t.get("meta") or {}
                tmin = {"url": t["url"], "title": t.get("title"), "kind": "transcript", "evidence": t.get("evidence"),
                        "published": meta.get("meeting_date") or t.get("published"), "fetched_at": t.get("fetched_at"),
                        "meta": {"body": meta.get("body"), "meeting_date": meta.get("meeting_date")}}
                for ln in (t.get("text") or "").splitlines():
                    if ":" not in ln[:80] and ">" not in ln[:80]:
                        continue                      # no speaker label: skip the regex
                    m = SPEAKER.match(ln)
                    if not m:
                        continue
                    said = m.group("said").strip()
                    if not (25 <= len(said) <= 600) or not STANCE_CUES.search(said):
                        continue
                    name = m.group("name").lower()
                    idx.setdefault(name.split()[-1], []).append((tmin, name, said))
            self._idx[jid] = idx
        return self._idx[jid].get(last, [])


def _clean_org(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" ,.;:'’\"“”")
    s = re.sub(r"^(?:the|a|an)\s+", "", s, flags=re.I)
    return s


def _split_names(tail: str) -> list[str]:
    """'State Chamber of Commerce and Sheriff Dana Reed. I ...' -> both names (stops at the sentence end)."""
    tail = re.split(r"[.;:!?\n]|\s+(?:for|in|on|at|to|because|who|which)\s+", tail, maxsplit=1)[0]
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", tail)
    out = []
    for p in parts:
        p = _clean_org(p)
        if p and p[0].isupper() and 3 <= len(p) <= 80:
            out.append(p)
    return out


def _d10(r: dict) -> str:
    return (r.get("published") or r.get("fetched_at") or "")[:10]


ORD_VOTE = re.compile(r"\b(ordinance|resolution)\b", re.I)


class SourceList:
    """The profile's sources[] with {t,u,d}; src indexes are 0-based."""

    def __init__(self):
        self.items: list[dict] = []
        self.index: dict[str, int] = {}

    def add(self, r: dict, title: str | None = None) -> int:
        u = r.get("url") or ""
        k = re.sub(r"^https?://(www\.)?", "", u).rstrip("/").lower()
        if k in self.index:
            return self.index[k]
        self.index[k] = len(self.items)
        self.items.append({"t": (title or r.get("title") or "").strip()[:200] or u, "u": u, "d": _d10(r),
                           "kind": r.get("kind"), "evidence": r.get("evidence") or "primary"})
        return self.index[k]


class Extractor:
    def __init__(self, cand: dict, root: Path | None = None):
        self.c = cand
        self.root = root or ROOT
        self.variants = cand.get("variants") or name_variants(cand["name"], cand.get("aliases", []))
        self.rx = name_regex(self.variants)
        self.last = (self.variants[0].split()[-1] if self.variants else cand["name"].split()[-1]).lower()
        self.first = (self.variants[0].split()[0] if self.variants else cand["name"].split()[0]).lower()

    def named(self, s: str) -> bool:
        """The sentence names the candidate: full name, or the surname alone (coverage switches to it after the
        first mention) when the surname is distinctive enough."""
        if self.rx.search(s):
            return True
        return len(self.last) >= 4 and bool(re.search(r"\b" + re.escape(self.last) + r"\b", s, re.I))

    # ------------------------------------------------------------ positions and statements
    def own_sentences(self, text: str) -> list[str]:
        """Sentences from the candidate's own channel that take a stance."""
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s) or not STANCE_CUES.search(s):
                continue
            if ENDORSED_BY.search(s) and not FIRST_PERSON.search(s):
                continue
            out.append(s)
        return out

    def reported_sentences(self, text: str) -> list[tuple[str, bool]]:
        """(sentence, attributed) from coverage: the candidate named with a stance cue; attributed when a speech
        verb or a quotation ties the words to them."""
        out = []
        sents = sentences(text)
        for i, s in enumerate(sents):
            if NOISE_SENT.search(s) or not STANCE_CUES.search(s):
                continue
            named = self.named(s)
            if not named:
                # a quoted sentence right after one naming the candidate ("..." he said.)
                prev = sents[i - 1] if i else ""
                if not (prev and self.rx.search(prev) and re.search(r"[\"“]", s) and ATTRIB.search(s)):
                    continue
            attributed = bool(ATTRIB.search(s)) or bool(re.search(r"[\"“][^\"”]{12,}[\"”]", s))
            out.append((s, attributed))
        return out

    def transcript_utterances(self, text: str) -> list[str]:
        """Lines a transcript attributes to the candidate (speaker labels)."""
        out = []
        for ln in (text or "").splitlines():
            m = SPEAKER.match(ln)
            if not m:
                continue
            name = m.group("name").lower()
            if self.last in name and (self.first in name or len(name.split()) == 1 or name.endswith(self.last)):
                said = m.group("said").strip()
                if STANCE_CUES.search(said) and 25 <= len(said) <= 600:
                    out.append(said)
        return out

    # ------------------------------------------------------------ ideology, affiliations, endorsements, ratings
    def markers(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            about_cand = bool(own and FIRST_PERSON.search(s)) or self.named(s) or (own and not re.search(r"\b(endorse|opponent)\b", s, re.I))
            if not about_cand:
                continue
            m = SELF_DESC.search(s)
            if m and (own or re.search(r"describ\w+ (?:him|her|them)self|calls? (?:him|her|them)self|self-described|identif\w+ as", s, re.I)):
                out.append({"marker": "self-description", "phrase": m.group("label").lower(), "text": s[:300]})
            for lab, rx in IDEOLOGY_MARKERS.items():
                mm = re.search(rx, s)
                if mm and (own and FIRST_PERSON.search(s) or (not own and self.named(s) and ATTRIB.search(s))):
                    out.append({"marker": lab, "phrase": mm.group(0).lower(), "text": s[:300]})
        return out

    def affiliations(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            if not own and not self.named(s):
                continue
            for m in AFFIL.finditer(s):
                out.append({"org": _clean_org(m.group("org")), "role": m.group("role").lower(), "text": s[:300]})
            for m in SERVED.finditer(s):
                out.append({"org": _clean_org(m.group("org")), "role": "served on", "text": s[:300]})
        return out

    def endorsements(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if NOISE_SENT.search(s):
                continue
            named = self.named(s)
            if not own and not named:
                continue
            for rx in (ENDORSED_BY, ENDORSEMENT_OF):
                for m in rx.finditer(s):
                    for who in _split_names(s[m.start("who"):]):
                        out.append({"name": who, "direction": "received", "text": s[:300]})
            for m in X_ENDORSES.finditer(s):
                who, whom = _clean_org(m.group("who")), _clean_org(m.group("whom"))
                if self.named(whom):
                    out.append({"name": who, "direction": "received", "text": s[:300]})
                elif self.named(who) or (own and re.search(r"\b(i|we)\b", who, re.I)):
                    out.append({"name": whom, "direction": "given", "text": s[:300]})
        seen, uniq = set(), []
        for e in out:
            k = (e["name"].lower(), e["direction"])
            if e["name"] and len(e["name"]) > 2 and k not in seen and not self.rx.search(e["name"]):
                seen.add(k)
                uniq.append(e)
        return uniq

    def ratings(self, text: str, own: bool) -> list[dict]:
        out = []
        for s in sentences(text):
            if not own and not self.named(s):
                continue
            for rx in (RATING, RATING2):
                for m in rx.finditer(s):
                    org = _clean_org(m.group("org"))
                    if len(org) < 3 or re.search(r"\b(the|a|an)$", org, re.I):
                        continue
                    y = YEAR.search(s)
                    out.append({"org": org, "rating": m.group("rating").replace(" percent", "%").replace(" ", ""),
                                "year": y.group(1) if y else "", "text": s[:300]})
        return out


# ----------------------------------------------------------------------------- the profile supplement
def profile_supplement(cand: dict, store: Store, root: Path | None = None, outlets: set[str] | None = None,
                       money: dict | None = None, career: list[dict] | None = None, legis: dict | None = None,
                       speakers: TranscriptSpeakers | None = None, records: dict | None = None) -> dict:
    """Everything the crawler has on one subject, as profile items with src indexes.
    speakers: a TranscriptSpeakers shared across candidates (built on the fly when not given).
    records: the candidate's records by kind when the caller already read them ({} = none); else read here."""
    ex = Extractor(cand, root)
    speakers = speakers or TranscriptSpeakers(store)
    sid = f"subj:{cand['id']}"
    S = SourceList()
    positions, statements, markers, affils, endos, rates, votes, bills = [], [], [], [], [], [], [], []
    seen_pos: set[str] = set()

    def add_position(text: str, r: dict, basis: str, evidence: str, review: bool = False):
        key = re.sub(r"[^a-z]", "", text.lower())[:120]
        if key in seen_pos:
            return
        seen_pos.add(key)
        topics = issues_of(text, root)
        i = S.add(r)
        item = {"stance": text[:420], "d": _d10(r), "src": [i], "basis": basis, "evidence": evidence}
        if review:
            item["needs_review"] = True
        if topics:
            for t in topics[:2]:
                positions.append({"topic": t, **item})
        else:
            statements.append({"d": _d10(r), "text": text[:420], "src": [i], "basis": basis, "evidence": evidence})

    def by_kind(kind: str) -> list[dict]:
        rows = records.get(kind, []) if records is not None else store.read(sid, kind)
        return sorted(rows, key=_d10, reverse=True)

    # own channels
    for r in by_kind("statement") + by_kind("post"):
        if r.get("evidence") == "unverified":
            continue
        basis = "campaign site" if r["kind"] == "statement" else f"verified {r.get('meta', {}).get('platform') or 'social'} post"
        for s in ex.own_sentences(r.get("text") or "")[:40]:
            add_position(s, r, basis, "primary")
        for m in ex.markers(r.get("text") or "", own=True):
            markers.append({**m, "d": _d10(r), "src": [S.add(r)], "basis": basis, "evidence": "primary"})
        for a in ex.affiliations(r.get("text") or "", own=True):
            affils.append({**a, "d": _d10(r), "src": [S.add(r)], "basis": basis, "evidence": "primary"})
        for e in ex.endorsements(r.get("text") or "", own=True):
            endos.append({**e, "d": _d10(r), "src": [S.add(r)], "basis": basis + " (self-reported)", "evidence": "primary"})
        for rt in ex.ratings(r.get("text") or "", own=True):
            rates.append({**rt, "src": [S.add(r)], "basis": basis + " (self-reported)", "evidence": "primary"})
    # what they said at meetings: transcripts in their targets where they are the labeled speaker
    jids = [j for j in cand.get("targets", []) if j]
    per_transcript: dict[str, int] = {}
    for jid in jids:
        for tmin, name, said in speakers.lines(jid, ex.last):
            if not (ex.first in name or len(name.split()) == 1 or name.endswith(ex.last)):
                continue
            if per_transcript.get(tmin["url"], 0) >= 30 or (tmin["url"] not in per_transcript and len(per_transcript) >= 400):
                continue
            per_transcript[tmin["url"]] = per_transcript.get(tmin["url"], 0) + 1
            meta = tmin["meta"]
            label = f"said at {meta.get('body') or 'a meeting'} {meta.get('meeting_date') or _d10(tmin)}".strip()
            add_position(said, tmin, label, "primary", review=True)
    # coverage by listed outlets (reported): named + attributed
    for r in by_kind("article"):
        if r.get("evidence") != "reported":
            continue
        outlet = r.get("meta", {}).get("outlet") or ""
        for s, attributed in ex.reported_sentences(r.get("text") or "")[:12]:
            if attributed:
                add_position(s, r, f"reported by {outlet}".strip(), "reported")
        for m in ex.markers(r.get("text") or "", own=False):
            markers.append({**m, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for a in ex.affiliations(r.get("text") or "", own=False):
            affils.append({**a, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for e in ex.endorsements(r.get("text") or "", own=False):
            endos.append({**e, "d": _d10(r), "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
        for rt in ex.ratings(r.get("text") or "", own=False):
            rates.append({**rt, "src": [S.add(r)], "basis": f"reported by {outlet}", "evidence": "reported"})
    # questionnaires and guides (kind guide_answer, from opencrawl.guides) and endorsement records
    for r in by_kind("guide_answer"):
        m = r.get("meta", {})
        q, a = m.get("question") or "", r.get("text") or ""
        if a:
            add_position(f"{q.strip()} — {a.strip()}" if q else a.strip(), r, f"questionnaire: {m.get('guide') or r.get('title')}",
                         r.get("evidence") or "primary")
    for r in by_kind("connection"):
        m = r.get("meta", {})
        if m.get("type") == "endorsement" and m.get("party"):
            endos.append({"name": m["party"], "direction": m.get("direction", "received"), "text": (r.get("text") or "")[:300],
                          "d": _d10(r), "src": [S.add(r)], "basis": m.get("basis") or ("campaign site (self-reported)" if m.get("self_reported") else "endorser's own page"),
                          "evidence": r.get("evidence") or "primary"})
        elif m.get("type") in ("membership", "affiliation") and m.get("party"):
            affils.append({"org": m["party"], "role": m.get("role", "member"), "text": (r.get("text") or "")[:300], "d": _d10(r),
                           "src": [S.add(r)], "basis": m.get("basis") or "record", "evidence": r.get("evidence") or "primary"})
        elif m.get("type") == "rating" and m.get("party"):
            rates.append({"org": m["party"], "rating": m.get("rating", ""), "year": m.get("year", ""), "src": [S.add(r)],
                          "basis": m.get("basis") or "scorecard", "evidence": r.get("evidence") or "primary"})
    # votes (council roll calls found for this candidate; legislature votes passed in)
    ords_for, ords_against = [], []
    # the newest 400 roll calls, plus every vote on an ordinance or resolution (those are the "supported/opposed" lists)
    if records is not None:
        allv = sorted(records.get("vote", []), key=lambda r: ((r.get("published") or ""), r["id"]), reverse=True)
        recent = allv[:400]
        seen_ids = {r["id"] for r in recent}
        on_ords = [r for r in allv[400:] if ORD_VOTE.search(f"{r.get('title') or ''} {(r.get('meta') or {}).get('ordinance_ref') or ''}")][:600]
    else:
        recent = store.read_recent(sid, "vote", 400)
        seen_ids = {r["id"] for r in recent}
        on_ords = [r for r in store.read_recent(sid, "vote", 600, "lower(title) LIKE '%ordinance%' OR lower(title) LIKE '%resolution%' OR "
                                                           "meta LIKE '%\"ordinance_ref\": \"Ordinance%' OR "
                                                           "meta LIKE '%\"ordinance_ref\": \"Resolution%'")
                   if r["id"] not in seen_ids]
    for r in recent + on_ords:
        in_list = r["id"] in seen_ids
        m = r.get("meta", {})
        v = (m.get("vote") or "").lower()
        item = {"bill": m.get("matter_file") or "", "session": r.get("target") or cand.get("target") or "",
                "title": (r.get("title") or "")[:200], "vote": m.get("vote"), "d": _d10(r), "src": [S.add(r)],
                "result": m.get("result"), "topics": m.get("topics", []), "basis": m.get("basis") or "roll call"}
        if m.get("procedural"):
            item["procedural"] = True
        if in_list:
            votes.append(item)
        is_ord = bool(re.search(r"\b(ordinance|resolution)\b", f"{item['bill']} {item['title']}", re.I)) and not m.get("procedural")
        if is_ord and v in ("yes", "yea", "aye", "for"):
            ords_for.append(item)
        elif is_ord and v in ("no", "nay", "against"):
            ords_against.append(item)
    for r in by_kind("sponsorship"):
        m = r.get("meta", {})
        item = {"number": m.get("number") or m.get("MatterFile") or "", "session": m.get("session") or r.get("target") or "",
                "caption": (r.get("title") or "")[:200], "role": m.get("role") or "sponsor", "d": _d10(r), "src": [S.add(r)],
                "enacted": m.get("enacted"), "status": m.get("status")}
        bills.append(item)
        if re.search(r"\b(ordinance|resolution)\b", f"{item['number']} {item['caption']}", re.I):
            ords_for.append({**item, "bill": item["number"], "title": item["caption"], "vote": "sponsor"})
    if legis:
        member = legis.get("member") or {}
        msrc = S.add({"url": member.get("u"), "title": member.get("t") or f"Legislature member page: {member.get('name')}", "kind": "legislator",
                      "evidence": "primary"}) if member.get("u") else None
        for v in legis.get("votes", []):
            votes.append({**v, "src": [S.add({"url": v.get("u"), "title": f"{v.get('bill')}: {v.get('title')}", "kind": "vote", "evidence": "primary"})] if v.get("u") else []})
        for b in legis.get("bills", []):
            bills.append({**b, "src": [S.add({"url": b.get("u"), "title": f"{b.get('number')}: {b.get('caption')}", "kind": "bill", "evidence": "primary"})] if b.get("u") else []})
        for cm in legis.get("committees", []):
            if cm.get("name"):
                affils.append({"org": f"{member.get('chamber') or 'General Assembly'} {cm['name']} Committee", "role": (cm.get("role") or "member").lower(),
                               "text": f"{cm.get('role') or 'Member'}, {cm['name']} ({cm.get('session') or ''})".strip(), "d": "",
                               "src": [msrc] if msrc is not None else [], "basis": "legislature member record", "evidence": "primary"})
        if member.get("title"):
            affils.append({"org": f"{member.get('legislature') or 'Legislature'} {member.get('chamber') or ''} leadership".replace("  ", " "), "role": member["title"].lower(), "text": member["title"], "d": "",
                           "src": [msrc] if msrc is not None else [], "basis": "legislature member record", "evidence": "primary"})
    # dedupe lists
    def uniq(items, keyf):
        seen, out = set(), []
        for x in items:
            k = keyf(x)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    markers = uniq(markers, lambda m: (m["marker"], m["phrase"], m["text"][:60]))
    affils = uniq(affils, lambda a: (a["org"].lower(), a["role"]))
    endos = uniq(endos, lambda e: (e["name"].lower(), e["direction"]))
    rates = uniq(rates, lambda r: (r["org"].lower(), r["rating"], r.get("year", "")))
    party = cand.get("party") or (cand.get("last_race") or {}).get("party") or ""
    ideology, isrc = compose_ideology(party, markers, affils, rates, endos)
    n = len(positions) + len(statements) + len(rates) + (1 if endos else 0) + (1 if ideology else 0) + len(votes) + len(bills)
    depth = "full" if n >= 12 and positions else "partial" if n >= 4 else "thin" if n else "none"
    out = {
        "key": cand.get("key") or cand["id"], "subject_id": cand["id"], "name": cand["name"], "party": party,
        "as_of": max((x.get("d") or "" for x in S.items), default=""), "office": cand.get("office", ""), "targets": jids,
        "sources": S.items,
        "ideology": ideology, "isrc": isrc, "ideology_markers": markers[:40],
        "positions": positions[:80], "statements": statements[:40],
        "affiliations": affils[:40], "endorsements": [e["name"] + (f" ({e['d'][:4]})" if e.get("d") else "") for e in endos if e["direction"] == "received"][:40],
        "endorsement_items": endos[:60], "esrc": sorted({i for e in endos for i in e["src"]}),
        "ratings": [{"org": r["org"], "rating": r["rating"], "year": r.get("year", ""), "src": r["src"], "basis": r["basis"]} for r in rates][:40],
        "votes": votes[:300], "bills": bills[:120],
        "ordinances_supported": ords_for[:120], "ordinances_opposed": ords_against[:120],
        "money": money or {}, "career": career or [],
        "inference_inputs": n, "depth": depth,
        "note": "Items quote or cite records the crawler opened; evidence is primary (own channel / official record) or "
                "reported (listed outlet). Nothing here is an inference. needs_review marks speaker-labeled transcript lines.",
    }
    return out


def compose_ideology(party: str, markers: list[dict], affils: list[dict], rates: list[dict], endos: list[dict]) -> tuple[str, list[int]]:
    """One sourced line from documented markers only (no guessing from a party label)."""
    parts, src = [], []
    if party:
        parts.append({"D": "Democrat", "R": "Republican", "L": "Libertarian", "I": "Independent", "G": "Green"}.get(party, party))
    selfd = [m for m in markers if m["marker"] == "self-description"]
    if selfd:
        phr = sorted({m["phrase"] for m in selfd})
        parts.append("self-described " + " / ".join(phr[:3]))
        src += [i for m in selfd for i in m["src"]]
    lab = defaultdict(int)
    for m in markers:
        if m["marker"] != "self-description":
            lab[m["marker"]] += 1
            src += m["src"]
    if lab:
        parts.append("language used: " + ", ".join(f"{k} ({v})" for k, v in sorted(lab.items(), key=lambda kv: -kv[1])[:3]))
    cauc = [a for a in affils if re.search(r"caucus", a["org"], re.I)]
    if cauc:
        parts.append("caucus: " + "; ".join(sorted({a["org"] for a in cauc})[:3]))
        src += [i for a in cauc for i in a["src"]]
    if rates:
        parts.append("ratings: " + "; ".join(f"{r['org']} {r['rating']}" for r in rates[:4]))
        src += [i for r in rates[:4] for i in r["src"]]
    recv = [e for e in endos if e["direction"] == "received"]
    if recv:
        parts.append("endorsed by " + "; ".join(sorted({e["name"] for e in recv})[:5]))
        src += [i for e in recv[:5] for i in e["src"]]
    if len(parts) <= 1 and not src:
        return "", []
    return "; ".join(parts), sorted(set(src))
