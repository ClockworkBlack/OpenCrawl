"""Entity resolution for people and organizations across filings, registries and records.

The same donor appears as "SMITH, JOHN A", "John Smith" and "Smith, John (Acme Corp)";
the same PAC as "State Realtors PAC", "ST REALTORS POLITICAL ACTION COMMITTEE" and
"Realtors PAC of the State". This module gives each a stable entity id so money,
offices, endorsements and memberships can be joined into one graph.

Rules (deliberately conservative: a wrong merge is worse than a missed one)
- organizations: normalized name (legal suffixes, "the", punctuation dropped; common
  abbreviations expanded). Two org strings merge only when the normalized names match.
- people: normalized "first last" (middle names/initials, suffixes, titles dropped).
  Two person strings merge when the normalized names match AND nothing known
  contradicts (a different employer AND a different city is a split: both are kept
  as separate entities and flagged `ambiguous`).
- subjects are entities too (kind "subject"), keyed by every name variant the research module
  knows, so a candidate who is also a donor or an officer of an organization resolves
  to the same node.
Every entity keeps `aliases` (every spelling seen) and `seen_in` (record urls).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..names import name_variants

ORG_HINTS = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|llp|lp|pllc|pc|p\.c|corp|corporation|company|co|ltd|pac|political action committee|"
    r"committee|cmte|association|assn|assoc|society|fund|alliance|council|coalition|union|local \d+|federation|foundation|"
    r"partners|partnership|group|trust|club|chamber|realtors|builders|bank|holdings|enterprises|services|party|campaign|"
    r"friends of|citizens for|committee to elect|for (?:mayor|council|commission|senate|house|governor|school board)|"
    r"\d{3,} ?pac|industries|properties|development|developers|construction|contractors|engineering|consulting|"
    r"law firm|attorneys|hospital|health|medical|dental|church|ministries|university|college|school|district|"
    r"authority|cooperative|co-op|emc|utilities|energy|power|gas|oil|electric|realty|real estate|homes|farms|"
    r"brothers|& sons|and sons|international|national|american|united|first|county|city of|state of)\b", re.I)
PERSON_HINTS = re.compile(r"\b(mr|mrs|ms|dr|jr|sr|ii|iii|iv)\b\.?", re.I)
LEGAL_SUFFIX = re.compile(r"\b(inc|incorporated|llc|l\.l\.c|llp|lp|pllc|pc|p\.c|corp|corporation|co|company|ltd|limited)\b\.?", re.I)
ABBREV = {"assn": "association", "assoc": "association", "cmte": "committee", "intl": "international",
          "natl": "national", "amer": "american", "bldg": "building", "mfg": "manufacturing", "svcs": "services",
          "svc": "service", "hosp": "hospital", "med": "medical", "dept": "department", "univ": "university",
          "pol": "political", "polit": "political", "pol.": "political", "fdn": "foundation", "comm": "committee",
          "bros": "brothers", "dev": "development", "const": "construction", "eng": "engineering", "ins": "insurance",
          "fin": "financial", "mgmt": "management", "mgt": "management", "ent": "enterprises", "prop": "properties",
          "props": "properties", "assocs": "associates", "ptnrs": "partners", "grp": "group", "hldgs": "holdings"}
PAC_WORDS = re.compile(r"\b(pac|political action committee|committee to elect|friends of|citizens for|for (?:mayor|council|"
                       r"commission|senate|house|governor|school board|commissioner|sheriff|judge)|campaign|victory fund|"
                       r"leadership fund|party)\b", re.I)


@lru_cache(maxsize=262144)
def is_org(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    if n.count(",") == 1 and not ORG_HINTS.search(n):      # "LAST, FIRST" style
        return False
    words = re.findall(r"[A-Za-z]+", n)
    if ORG_HINTS.search(n):
        return True
    if len(words) >= 5:
        return True
    if n.isupper() and len(words) >= 4:
        return True
    return False


COMMITTEE_RX = [
    re.compile(r"^(?:the\s+)?(?:friends|committee|citizens|neighbors|people|residents|voters|\w+ans)\s+(?:of|to\s+elect|to\s+re-?elect|for)\s+(?P<name>[A-Za-z.'’\- ]{4,50}?)(?:\s+(?:for|as)\s+.*)?$", re.I),
    re.compile(r"^(?:the\s+)?(?:re-?)?elect\s+(?P<name>[A-Za-z.'’\- ]{4,50}?)(?:\s+(?:for|as|to)\s+.*)?$", re.I),
    re.compile(r"^(?P<name>[A-Za-z.'’\- ]{4,50}?)\s+(?:for|4)\s+(?:mayor|council|city council|commission|commissioner|senate|state senate|house|"
               r"state house|governor|lt\.? governor|school board|sheriff|judge|congress|psc|district attorney|solicitor|"
               r"clerk|coroner|assessor|treasurer|auditor|recorder|us senate|u\.s\. senate|the people|[A-Z][a-z]+)\b.*$", re.I),
    re.compile(r"^(?P<name>[A-Za-z.'’\- ]{4,50}?)\s+(?:campaign|campaign committee|victory fund|leadership fund)\s*$", re.I),
]


STOP = {"a", "an", "the", "better", "our", "new", "good", "strong", "safe", "great", "stronger", "safer", "progress", "future", "change", "team"}


@lru_cache(maxsize=262144)
def committee_candidate(name: str) -> str | None:
    """'Friends of Robin Ashby' / 'Robin Ashby for Mayor' / 'Committee to Elect Robin Ashby' -> 'Robin Ashby'."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    for rx in COMMITTEE_RX:
        m = rx.match(n)
        if m:
            person = m.group("name").strip(" ,")
            toks = person.split()
            if (2 <= len(toks) <= 4 and not ORG_HINTS.search(person) and all(t[0].isupper() for t in toks)
                    and not any(t.lower() in STOP for t in toks)):
                return display_person(person) or person
    return None


@lru_cache(maxsize=262144)
def norm_org(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"\bpolitical action committee\b", "pac", n)
    n = LEGAL_SUFFIX.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    toks = [ABBREV.get(t, t) for t in n.split()]
    toks = [t for t in toks if t not in ("the", "of", "and", "for", "a", "an")]
    return " ".join(toks)


@lru_cache(maxsize=262144)
def norm_person(name: str) -> str:
    v = name_variants(name)
    if not v:
        return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()
    w = v[0].split()
    w = [x for x in w if len(x.rstrip(".")) > 1]  # drop initials
    first, last = w[0], w[-1]
    return f"{first} {last}".lower() if len(w) >= 2 else v[0].lower()


@lru_cache(maxsize=262144)
def display_person(name: str) -> str:
    v = name_variants(name)
    return v[0] if v else (name or "").strip()


class EntityIndex:
    """In-memory index persisted in the database (kv "entities"), so entity ids stay stable from run to run without
    a large file committed every night; a path is the fallback (and the migration source) when there's no store."""

    KV = "entities"

    def __init__(self, path: Path | None = None, store=None):
        self.path, self.store = path, store
        self.entities: dict[str, dict] = {}
        self._by_key: dict[tuple[str, str], list[str]] = {}
        self._by_candidate: dict[str, str] = {}
        self._n = 0
        raw = store.kv_get(self.KV) if store is not None else None
        if raw is None and path and path.exists():
            raw = path.read_text(encoding="utf-8")
        if raw:
            try:
                d = json.loads(raw)
                self.entities = d.get("entities", {})
                self._n = d.get("n", len(self.entities))
                for eid, e in self.entities.items():
                    e.setdefault("aliases", [])
                    e.setdefault("keys", [])
                    e.setdefault("seen_in", [])
                    e.setdefault("attrs", {})
                    for k in e["keys"]:
                        self._by_key.setdefault((e["kind"], k), []).append(eid)
                    if e["kind"] == "subject" and e["attrs"].get("subject_id"):
                        self._by_candidate.setdefault(e["attrs"]["subject_id"], eid)
            except Exception:  # noqa: BLE001
                self.entities = {}

    def flush(self):
        doc = json.dumps({"n": self._n, "entities": self.entities}, ensure_ascii=False, separators=(",", ":"))
        if self.store is not None:
            self.store.kv_set(self.KV, doc)
            self.store.flush()
        elif self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(doc, encoding="utf-8")

    # ---------------------------------------------------------------- resolution
    def _new(self, kind: str, name: str, keys: list[str], attrs: dict | None = None) -> str:
        self._n += 1
        eid = f"{kind[0]}{self._n}"
        self.entities[eid] = {"id": eid, "kind": kind, "name": name.strip(), "aliases": [name.strip()], "keys": list(keys),
                              "attrs": dict(attrs or {}), "seen_in": []}
        for k in keys:
            self._by_key.setdefault((kind, k), []).append(eid)
        return eid

    def _merge_attrs(self, e: dict, attrs: dict | None, name: str, url: str | None):
        for k, v in (attrs or {}).items():
            if v in (None, ""):
                continue
            if k in ("employer", "city", "state", "occupation"):
                e["attrs"].setdefault(k + "s", [])
                vv = str(v).strip()
                if vv.lower() not in [x.lower() for x in e["attrs"][k + "s"]]:
                    e["attrs"][k + "s"].append(vv)
                e["attrs"].setdefault(k, vv)
            else:
                e["attrs"].setdefault(k, v)
        if name.strip() and name.strip() not in e["aliases"]:
            e["aliases"].append(name.strip())
        if url and url not in e["seen_in"] and len(e["seen_in"]) < 50:
            e["seen_in"].append(url)

    def resolve(self, name: str, kind: str | None = None, attrs: dict | None = None, url: str | None = None) -> str | None:
        """Entity id for a name (created if new). kind: person | org | subject | committee, or None to guess."""
        name = re.sub(r"\s+", " ", (name or "").strip(" ,.;:"))
        if len(name) < 3:
            return None
        attrs = attrs or {}
        if kind is None:
            kind = "org" if is_org(name) else "person"
        if kind == "org":
            key = norm_org(name)
            if not key:
                return None
            for k2 in ("committee", "org"):
                ids = self._by_key.get((k2, key)) or []
                if ids:
                    self._merge_attrs(self.entities[ids[0]], attrs, name, url)
                    return ids[0]
            if PAC_WORDS.search(name):
                kind = "committee"
            eid = self._new(kind, name, [key], attrs)
        elif kind == "committee":
            key = norm_org(name)
            ids = self._by_key.get(("committee", key)) or self._by_key.get(("org", key)) or []
            if ids:
                self._merge_attrs(self.entities[ids[0]], attrs, name, url)
                return ids[0]
            eid = self._new("committee", name, [key], attrs)
        else:  # person or candidate
            key = norm_person(name)
            if not key or " " not in key:
                return None
            ids = self._by_key.get(("subject", key)) or []
            if not ids:
                ids = self._by_key.get(("person", key)) or []
            chosen = None
            for eid in ids:
                e = self.entities[eid]
                if not self._contradicts(e, attrs):
                    chosen = eid
                    break
            if chosen:
                self._merge_attrs(self.entities[chosen], attrs, name, url)
                if ids and len(ids) > 1:
                    self.entities[chosen]["ambiguous"] = True
                return chosen
            if ids:  # same name, contradicting employer and city: a different person
                for eid in ids:
                    self.entities[eid]["ambiguous"] = True
            eid = self._new(kind if kind in ("person", "subject") else "person", display_person(name) or name, [key], attrs)
            if ids:
                self.entities[eid]["ambiguous"] = True
        self._merge_attrs(self.entities[eid], attrs, name, url)
        return eid

    @staticmethod
    def _contradicts(e: dict, attrs: dict) -> bool:
        emp, city = (attrs.get("employer") or "").lower(), (attrs.get("city") or "").lower()
        emps = [x.lower() for x in e["attrs"].get("employers", [])]
        cities = [x.lower() for x in e["attrs"].get("citys", [])]
        if emp and emps and city and cities:
            return emp not in emps and city not in cities
        return False

    def add_subject(self, c: dict) -> str:
        keys = [norm_person(v) for v in (c.get("variants") or name_variants(c["name"], c.get("aliases", [])))]
        keys = [k for k in dict.fromkeys(keys) if " " in k]
        for k in keys:
            for eid in self._by_key.get(("subject", k)) or []:
                e = self.entities[eid]
                if e["attrs"].get("subject_id") in (None, c["id"]):
                    e["attrs"]["subject_id"] = c["id"]
                    self._by_candidate[c["id"]] = eid
                    for k2 in keys:
                        if k2 not in e["keys"]:
                            e["keys"].append(k2)
                            self._by_key.setdefault(("subject", k2), []).append(eid)
                    return eid
            # a plain person already seen with this name becomes the candidate node
            for eid in list(self._by_key.get(("person", k)) or []):
                e = self.entities[eid]
                if e["kind"] == "person" and not e.get("ambiguous"):
                    e["kind"] = "subject"
                    e["attrs"]["subject_id"] = c["id"]
                    self._by_candidate[c["id"]] = eid
                    for k2 in keys:
                        self._by_key.setdefault(("subject", k2), []).append(eid)
                        if k2 not in e["keys"]:
                            e["keys"].append(k2)
                    return eid
        eid = self._new("subject", c["name"], keys, {"subject_id": c["id"],
                                                        "target": c.get("target"), "office": c.get("office")})
        self._by_candidate[c["id"]] = eid
        return eid

    def get(self, eid: str) -> dict | None:
        return self.entities.get(eid)

    def by_subject(self, subject_id: str) -> str | None:
        eid = self._by_candidate.get(subject_id)
        if eid and self.entities.get(eid, {}).get("kind") == "subject":
            return eid
        for eid, e in self.entities.items():   # (an index built before this map existed)
            if e["kind"] == "subject" and e["attrs"].get("subject_id") == subject_id:
                self._by_candidate[subject_id] = eid
                return eid
        return None

    def find(self, name: str, kind: str | None = None) -> str | None:
        """Lookup without creating."""
        if kind in (None, "org", "committee"):
            key = norm_org(name)
            for k2 in ("committee", "org"):
                ids = self._by_key.get((k2, key)) or []
                if ids:
                    return ids[0]
        if kind in (None, "person", "subject"):
            key = norm_person(name)
            for k2 in ("subject", "person"):
                ids = self._by_key.get((k2, key)) or []
                if ids:
                    return ids[0]
        return None
