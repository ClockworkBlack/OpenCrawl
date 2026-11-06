"""Campaign finance: who gives, who receives, and what the organizations are.

Where the money records come from
  imports/finance/*.csv       exports from a state campaign-finance system (or a clerk's spreadsheet). A file
                              named <subject-id>.csv is that subject's; any other file needs a recipient column.
  crawled disclosure reports  campaign contribution disclosure reports that a city or county posts
                              (doc_type finance_report): the PDF text is parsed into contributions,
                              expenditures, loans and in-kind items. Parsed-from-PDF rows are flagged
                              needs_review (layouts vary).
  FEC API                     federal candidates and PACs: committees, itemized receipts (schedule A),
                              disbursements (schedule B: who a PAC gives to), committee details
                              (type, designation, connected organization, treasurer).
  the state system's portal   config/finance.yaml `state_commission.search_url` is used only when a
                              person has confirmed the URL template (the crawler never guesses one);
                              `probe_commission` lists the links the portal's front page offers so that
                              can be confirmed.

config/finance.yaml also names the state (`state: CO`), which scopes the FEC, IRS and OpenCorporates lookups.

What an organization is (kind organization, one record per resolved entity)
  FEC committee details, ProPublica Nonprofit Explorer (IRS 990 data: subsection, NTEE activity
  code, revenue), OpenCorporates (with an API key: incorporation, officers), the organization's
  own site (config/organizations.yaml websites: the about page and any officer/board list), and
  hand notes in that file. Every field carries the url it came from.

Who runs them / who is part of them: treasurers and connected organizations (FEC), officers
(OpenCorporates, org sites, hand notes). Who receives their money: schedule B for FEC
committees; for state PACs, their own disclosure reports when posted or imported.

Everything here is primary evidence (official filings and registries) except org-site text,
which is the organization's own description of itself (evidence primary, basis self-description).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import yaml

from .entities import EntityIndex, committee_candidate, is_org
from ..fetch import Blocked, Disallowed, Fetcher
from ..store import Record, Store

FEC = "https://api.open.fec.gov/v1"
PROPUBLICA = "https://projects.propublica.org/nonprofits/api/v2"
OPENCORPORATES = "https://api.opencorporates.com/v0.4"

DATE_RX = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
ISO_RX = re.compile(r"\b(20\d\d|19\d\d)-(\d\d)-(\d\d)\b")
AMOUNT_RX = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?\b")
SECTION_RX = re.compile(r"^\s*(?:(?:schedule|section|part)\s+[A-Z0-9]+[:.\s-]*)?(?P<sec>contributions?(?:\s+received)?|"
                        r"itemized\s+contributions?|in[- ]kind(?:\s+contributions?)?|expenditures?|disbursements?|"
                        r"itemized\s+expenditures?|loans?(?:\s+received)?|investments?|common\s+source|"
                        r"summary|totals?)\b", re.I)
HEADER_FIELDS = {
    "candidate": re.compile(r"\b(?:candidate|filer)(?:'s)?\s+name\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "committee": re.compile(r"\b(?:committee|pac|organization)\s+name\s*[:\-]\s*(?P<v>[^\n]{3,100})", re.I),
    "office": re.compile(r"\boffice\s+sought\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "period": re.compile(r"\b(?:reporting|report)\s+period\s*[:\-]\s*(?P<v>[^\n]{5,80})", re.I),
    "report_type": re.compile(r"\breport\s+type\s*[:\-]\s*(?P<v>[^\n]{3,80})", re.I),
    "election_year": re.compile(r"\belection\s+year\s*[:\-]\s*(?P<v>20\d\d)", re.I),
}
_VAL = r"(?P<v>.+?)(?=\s{2,}|\s+(?:employer|occupation|date|amount|address|election|description|purpose|city|state|zip)\s*:|\n|\||$)"
LABEL_RX = {
    "name": re.compile(r"(?:contributor|payee|lender|vendor|name)\s*(?:name)?\s*[:\-]\s*" + _VAL, re.I),
    "employer": re.compile(r"employer\s*[:\-]\s*" + _VAL, re.I),
    "occupation": re.compile(r"occupation\s*[:\-]\s*" + _VAL, re.I),
    "address": re.compile(r"address\s*[:\-]\s*" + _VAL, re.I),
    "purpose": re.compile(r"(?:purpose|description)\s*[:\-]\s*" + _VAL, re.I),
    "election": re.compile(r"election\s*[:\-]\s*" + _VAL, re.I),
}
US_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
             "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR")
CITY_STATE_RX = re.compile(r"([A-Za-z.' ]{2,40}),?\s+(" + US_STATES + r")\b\.?\s*(\d{5})?")
NTEE = {"A": "arts, culture and humanities", "B": "education", "C": "environment", "D": "animal-related",
        "E": "health care", "F": "mental health", "G": "diseases and disorders", "H": "medical research",
        "I": "crime and legal", "J": "employment", "K": "food, agriculture and nutrition", "L": "housing and shelter",
        "M": "public safety and disaster relief", "N": "recreation and sports", "O": "youth development",
        "P": "human services", "Q": "international affairs", "R": "civil rights, social action and advocacy",
        "S": "community improvement and capacity building", "T": "philanthropy and grantmaking",
        "U": "science and technology", "V": "social science", "W": "public and societal benefit",
        "X": "religion", "Y": "mutual and membership benefit", "Z": "unknown"}
SUBSECTION = {"3": "501(c)(3) charity (no political campaign activity allowed)",
              "4": "501(c)(4) social welfare organization (may lobby and do some political activity)",
              "5": "501(c)(5) labor or agricultural organization", "6": "501(c)(6) business league or chamber of commerce",
              "7": "501(c)(7) social or recreational club", "8": "501(c)(8) fraternal beneficiary society",
              "19": "501(c)(19) veterans organization", "27": "527 political organization"}


def _d10(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    m = ISO_RX.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = DATE_RX.search(s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if len(y) == 4 else (2000 + int(y) if int(y) < 70 else 1900 + int(y))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _amount(s: str | None) -> float | None:
    if s is None:
        return None
    t = re.sub(r"[^\d.\-()]", "", str(s))
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# ----------------------------------------------------------------------------- CSV exports
def parse_finance_csv(path: Path) -> list[dict]:
    """Tolerant reader for campaign-finance exports: finds contributor, amount, date, employer, recipient columns by name."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}

        def col(*keys):
            for k in keys:
                for lc, orig in cols.items():
                    if k == lc or k in lc:
                        return orig
            return None

        name_c = col("contributor name", "contributor", "payee", "lender", "name")
        amt_c, date_c = col("amount", "total"), col("date")
        emp_c, occ_c, city_c = col("employer"), col("occupation"), col("city")
        state_c, type_c = col("state"), col("type", "category", "kind")
        rec_c = col("recipient", "candidate name", "candidate", "committee name", "filer")
        purpose_c, elec_c = col("purpose", "description"), col("election")
        for r in reader:
            if not name_c or not (r.get(name_c) or "").strip():
                continue
            amt = _amount(r.get(amt_c)) if amt_c else None
            kind = (r.get(type_c) or "").strip().lower() if type_c else ""
            rows.append({"name": r.get(name_c, "").strip(), "amount": amt, "date": _d10(r.get(date_c)) if date_c else None,
                         "employer": (r.get(emp_c) or "").strip() if emp_c else "",
                         "occupation": (r.get(occ_c) or "").strip() if occ_c else "",
                         "city": (r.get(city_c) or "").strip() if city_c else "",
                         "state": (r.get(state_c) or "").strip() if state_c else "",
                         "kind": _kind_of(kind), "recipient": (r.get(rec_c) or "").strip() if rec_c else "",
                         "purpose": (r.get(purpose_c) or "").strip() if purpose_c else "",
                         "election": (r.get(elec_c) or "").strip() if elec_c else ""})
    return rows


def _kind_of(s: str) -> str:
    s = (s or "").lower()
    if re.search(r"expend|disburse|payment|paid", s):
        return "expenditure"
    if "loan" in s:
        return "loan"
    if "in-kind" in s or "in kind" in s or "inkind" in s:
        return "in_kind"
    return "contribution"


# ----------------------------------------------------------------------------- CCDR text (PDF)
def looks_like_finance_report(title: str, url: str, text: str) -> bool:
    s = f"{title} {url}".lower()
    head = (text or "")[:3000].lower()
    if re.search(r"campaign[ _-]?(contribution|finance|disclosure)|contribution[ _-]?disclosure|\bccdr\b", s):
        return True
    return bool(re.search(r"campaign contribution disclosure report|contributions received|itemized contributions", head)
                and re.search(r"\b(contributor|employer|occupation|amount)\b", head))


def header_of(text: str) -> dict:
    head = (text or "")[:4000]
    out = {}
    for k, rx in HEADER_FIELDS.items():
        m = rx.search(head)
        if m:
            out[k] = re.sub(r"\s+", " ", m.group("v")).strip(" :.-")
    return out


def _pick_amount(line: str, avoid: tuple[int, int]) -> tuple[float | None, int, int]:
    """The money amount on a line: prefers $-prefixed / decimal figures, skips the date, zips and street numbers."""
    best = None
    for m in AMOUNT_RX.finditer(line):
        if m.end() > avoid[0] and m.start() < avoid[1]:
            continue
        raw = m.group(0)
        digits = m.group(1)
        if re.fullmatch(r"\d{5}", digits) and not m.group(2):       # zip code
            continue
        tail = line[m.end():m.end() + 12]
        if not m.group(2) and "$" not in raw and "," not in digits and re.match(r"\s+[A-Za-z]", tail) and not re.match(r"\s*(?:general|primary|runoff|special)", tail, re.I):
            continue                                                 # "3200 Presidential Dr": a street number
        v = _amount(raw)
        if v is None or v < 1:
            continue
        strong = "$" in raw or bool(m.group(2)) or "," in digits
        cand = (strong, m.start(), v, m)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return (best[2], best[3].start(), best[3].end()) if best else (None, -1, -1)


def _header_columns(line: str) -> dict | None:
    low = line.lower()
    if "amount" in low and ("name" in low or "contributor" in low or "payee" in low) and ("date" in low):
        return {"occ_emp": "occupation" in low and "employer" in low, "purpose": "purpose" in low or "description" in low,
                "election": "election" in low}
    return None


SKIP_NAME = re.compile(r"^(?:report|reporting|period|date|candidate|office|committee|total|sub-?total|page|balance|sum|cash|"
                       r"beginning|ending|net|name|contributor|payee)\b|:$", re.I)


def parse_ccdr_text(text: str) -> list[dict]:
    """Rows of {name, amount, date, employer, occupation, city, state, kind, purpose, election, line} from report text.
    Two layouts: labeled blocks ('Contributor Name: ... Employer: ... Date: ... Amount: ...') and table rows
    (name ... date ... amount on one line, with employer/occupation on the same or following lines)."""
    rows: list[dict] = []
    section = "contribution"
    cols: dict = {"occ_emp": True, "purpose": True, "election": True}
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        sm = SECTION_RX.match(ln)
        if sm and len(ln.strip()) < 60:
            sec = sm.group("sec").lower()
            section = ("expenditure" if re.search(r"expend|disburs", sec) else "loan" if "loan" in sec
                       else "in_kind" if "kind" in sec else "contribution" if "contribution" in sec else section)
            if re.search(r"summary|total|common|investment", sec):
                section = "skip"
            i += 1
            continue
        hc = _header_columns(ln)
        if hc:
            cols = hc
            i += 1
            continue
        if section == "skip" or not ln.strip():
            i += 1
            continue
        lab = LABEL_RX["name"].search(ln)
        if lab and re.match(r"\s*(?:contributor|payee|lender|vendor)", ln, re.I):
            j = i + 1
            while j < len(lines) and j < i + 10 and lines[j].strip() and not re.match(r"\s*(?:contributor|payee|lender|vendor)", lines[j], re.I):
                j += 1
            block = "\n".join(lines[i:j])
            row = {"name": lab.group("v").strip(), "kind": section, "line": i + 1}
            for k in ("employer", "occupation", "address", "purpose", "election"):
                m = LABEL_RX[k].search(block)
                if m:
                    row[k] = m.group("v").strip()
            dm = re.search(r"date[^:\n]*[:\-]\s*([^\n|]+)", block, re.I)
            am = re.search(r"amount[^:\n]*[:\-]\s*\$?\s*([\d,]+(?:\.\d{2})?)", block, re.I)
            row["date"] = _d10(dm.group(1)) if dm else None
            row["amount"] = _amount(am.group(1)) if am else None
            _city_state(row)
            if row["name"] and (row["amount"] is not None or row["date"]):
                rows.append(row)
            i = j
            continue
        dm = DATE_RX.search(ln) or ISO_RX.search(ln)
        if dm:
            amount, a0, a1 = _pick_amount(ln, (dm.start(), dm.end()))
            if amount is not None:
                before = ln[:min(dm.start(), a0)].strip(" |\t-")
                segs = [x.strip() for x in re.split(r"\s{2,}|\t|\|", before) if x.strip()]
                name = segs[0] if segs else ""
                if (not name or len(name) < 3) and i:
                    prev = lines[i - 1].strip()
                    if prev and not DATE_RX.search(prev) and len(prev) < 80 and not SECTION_RX.match(prev):
                        name = re.split(r"\s{2,}|\t|\|", prev)[0].strip()
                        segs = [name] + segs
                if name and not SKIP_NAME.search(name):
                    row = {"name": name.strip(" ,"), "amount": amount, "date": _d10(dm.group(0)), "kind": section,
                           "line": i + 1, "employer": "", "occupation": ""}
                    after = [x.strip() for x in re.split(r"\s{2,}|\t|\|", ln[max(dm.end(), a1):]) if x.strip()]
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    extra = []
                    if nxt and not DATE_RX.search(nxt) and not SECTION_RX.match(nxt) and len(nxt) < 120 and (
                            CITY_STATE_RX.search(nxt) or re.search(r"employer|occupation", nxt, re.I)):
                        extra = [x.strip() for x in re.split(r"\s{2,}|\t|\|", nxt) if x.strip()]
                    for seg in segs[1:] + after + extra:
                        if CITY_STATE_RX.search(seg) and re.search(r"\d", seg):
                            row.setdefault("address", seg)
                        elif cols.get("occ_emp") and "/" in seg and not row["employer"]:
                            occ, emp = (x.strip() for x in seg.split("/", 1))
                            row["occupation"], row["employer"] = occ, emp
                        elif re.match(r"(?:general|primary|runoff|special)\b", seg, re.I):
                            row["election"] = seg
                        elif LABEL_RX["employer"].search(seg):
                            row["employer"] = LABEL_RX["employer"].search(seg).group("v").strip()
                        elif LABEL_RX["occupation"].search(seg):
                            row["occupation"] = LABEL_RX["occupation"].search(seg).group("v").strip()
                        elif section == "expenditure" and not row.get("purpose"):
                            row["purpose"] = seg
                        elif section != "expenditure" and not row["occupation"] and len(seg) < 40 and not re.search(r"\d", seg):
                            row["occupation"] = seg
                    _city_state(row, " ".join(after + extra))
                    rows.append(row)
        i += 1
    return rows


def _city_state(row: dict, extra: str = ""):
    s = " ".join(x for x in (row.get("address", ""), extra) if x)
    m = CITY_STATE_RX.search(s)
    if m:
        row.setdefault("city", m.group(1).strip().title())
        row.setdefault("state", m.group(2).upper()[:2])


# ----------------------------------------------------------------------------- records
def contribution_record(source_id: str, url: str, row: dict, recipient: str, recipient_kind: str,
                        filing_source: str, target: str = "", needs_review: bool = False,
                        report: dict | None = None, channel: str = "import") -> Record:
    name = (row.get("name") or "").strip()
    kind = row.get("kind") or "contribution"
    label = {"contribution": "Contribution", "expenditure": "Expenditure", "loan": "Loan", "in_kind": "In-kind contribution"}[kind]
    title = f"{label}: {name}" + (f" ${row['amount']:,.2f}" if row.get("amount") is not None else "")
    key = f"{source_id}|contribution|{url}|{name.lower()}|{row.get('date') or ''}|{row.get('amount') or ''}|{row.get('line', '')}"
    return Record(source_id, "contribution", url, title=title[:200], text=json.dumps(row, ensure_ascii=False),
                  published=row.get("date"), channel=channel, evidence="primary", target=target, key=key,
                  meta={"type": kind, "name": name, "amount": row.get("amount"), "date": row.get("date"),
                        "employer": row.get("employer", ""), "occupation": row.get("occupation", ""),
                        "city": row.get("city", ""), "state": row.get("state", ""), "purpose": row.get("purpose", ""),
                        "election": row.get("election", ""), "recipient": recipient, "recipient_kind": recipient_kind,
                        "donor_is_org": is_org(name), "filing_source": filing_source, "needs_review": needs_review,
                        "report": report or {}, "opened": True})


# ----------------------------------------------------------------------------- the Finance runner
class Finance:
    def __init__(self, root: Path, cfg: dict, fetcher: Fetcher | None, store: Store, log=print):
        self.root, self.cfg, self.f, self.store, self.log = root, cfg, fetcher, store, log
        p = root / "config" / "finance.yaml"
        self.opts = (yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}) if p.exists() else {}
        self.state = (self.opts.get("state") or (cfg.get("civic") or {}).get("state_abbr") or "").upper()[:2]
        sc = self.opts.get("state_commission") or {}
        self.commission_name = sc.get("name") or (f"{self.state} campaign finance system" if self.state else "state campaign finance system")
        self.orgs_cfg = self._load_orgs()
        self.entities = EntityIndex(root / "state" / "entities.json", store=store)
        raw = store.kv_get("orgs")                       # organization lookups: when each was last checked
        legacy = root / "state" / "orgs.json"
        self.org_state = json.loads(raw) if raw else (json.loads(legacy.read_text()) if legacy.exists() else {})

    def _load_orgs(self) -> dict:
        p = self.root / "config" / "organizations.yaml"
        d = (yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}) if p.exists() else {}
        out = {}
        for o in d.get("organizations") or []:
            if o.get("name"):
                out[o["name"].lower()] = o
                for a in o.get("aliases") or []:
                    out[a.lower()] = o
        return out

    def flush(self):
        self.entities.flush()
        self.store.kv_set("orgs", json.dumps(self.org_state, ensure_ascii=False, separators=(",", ":")))
        self.store.flush()

    # ------------------------------------------------------------------ 1. imports
    def from_imports(self, candidates: list[dict]) -> int:
        by_id = {c["id"]: c for c in candidates}
        by_name = {}
        for c in candidates:
            for v in c.get("variants") or [c["name"]]:
                by_name[v.lower()] = c
        n = 0
        d = self.root / "imports" / "finance"
        if not d.exists():
            return 0
        for p in sorted(d.glob("*.csv")):
            cand = by_id.get(p.stem)
            for row in parse_finance_csv(p):
                rcp = cand
                if not rcp and row.get("recipient"):
                    rcp = by_name.get(row["recipient"].lower()) or by_name.get((committee_candidate(row["recipient"]) or "").lower())
                recipient = rcp["name"] if rcp else row.get("recipient", "")
                sid = f"subj:{rcp['id']}" if rcp else f"fin:import"
                rec = contribution_record(sid, f"file://{p.name}", row, recipient, "candidate" if rcp else "committee",
                                          f"{self.commission_name} export (import)",
                                          target=(rcp or {}).get("target", ""))
                if rcp:
                    rec.meta["subject_id"] = rcp["id"]
                if self.store.add_new(rec):
                    n += 1
        return n

    # ------------------------------------------------------------------ 2. crawled CCDR documents
    def from_documents(self, candidates: list[dict], limit: int = 500) -> dict:
        """Parse posted disclosure reports (documents with doc_type finance_report, or whose text looks like one)."""
        by_name = {}
        for c in candidates:
            for v in c.get("variants") or [c["name"]]:
                by_name[v.lower()] = c
        out = {"reports": 0, "rows": 0}
        rows = self.store.query("kind='document' AND (meta LIKE '%\"doc_type\": \"finance_report\"%' OR lower(title) LIKE "
                                "'%contribution%' OR lower(title) LIKE '%disclosure%') AND meta NOT LIKE '%\"finance_parsed\"%'",
                                limit=limit)
        for d in rows:
            text = d.get("text") or ""
            meta = dict(d["meta"])
            meta["finance_parsed"] = dt.date.today().isoformat()
            if looks_like_finance_report(d.get("title") or "", d["url"], text):
                head = header_of(text)
                who = head.get("candidate") or head.get("committee") or ""
                cand = by_name.get(who.lower()) or by_name.get((committee_candidate(who) or "").lower()) if who else None
                parsed = parse_ccdr_text(text)
                sid = f"subj:{cand['id']}" if cand else d["source_id"]
                for row in parsed:
                    rec = contribution_record(sid, d["url"], row, (cand or {}).get("name") or who,
                                              "candidate" if cand else ("committee" if who else "unknown"),
                                              "Campaign Contribution Disclosure Report posted by the target",
                                              target=d.get("target") or "", needs_review=True, report=head,
                                              channel="pdf")
                    if cand:
                        rec.meta["subject_id"] = cand["id"]
                    if self.store.add_new(rec):
                        out["rows"] += 1
                meta["finance_rows"] = len(parsed)
                meta["finance_filer"] = who
                out["reports"] += 1
            self.store.add(Record(d["source_id"], d["kind"], d["url"], title=d.get("title") or "", text=text,
                                  markdown=d.get("markdown") or "", published=d.get("published"), channel=d.get("channel") or "",
                                  target=d.get("target") or "", evidence=d.get("evidence") or "", meta=meta,
                                  key=d.get("key")))
        return out

    # ------------------------------------------------------------------ 3. FEC (federal)
    def _fec(self, path: str, params: dict | None = None) -> dict | None:
        if not self.f:
            return None
        key = os.environ.get("FEC_API_KEY", "DEMO_KEY")
        try:
            r = self.f.get(f"{FEC}{path}", params={**(params or {}), "api_key": key}, use_cache=False)
        except (Blocked, Disallowed):
            return None
        if r.status != 200:
            return None
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return None

    def fec_candidate_id(self, c: dict) -> str | None:
        """The subject's FEC id: given in config, else looked up once (by name and state) and remembered 90 days."""
        if c.get("fec_id"):
            return c["fec_id"]
        office = (c.get("office") or "").lower()
        if not re.search(r"u\.?s\.? (house|senate|representative|congress)|congress", office):
            return None
        cache = json.loads(self.store.kv_get("fec_ids") or "{}")
        hit = cache.get(c["id"])
        if hit and hit.get("checked", "") >= (dt.date.today() - dt.timedelta(days=90)).isoformat():
            return hit.get("fec_id") or None
        d = self._fec("/candidates/search/", {"q": c["name"], "per_page": 5, **({"state": self.state} if self.state else {})})
        found = None
        for r in (d or {}).get("results", []):
            if (r.get("name") or "").lower().split(",")[0].strip() == c["name"].split()[-1].lower():
                found = r.get("candidate_id")
                break
        if d is not None:
            cache[c["id"]] = {"fec_id": found or "", "checked": dt.date.today().isoformat()}
            self.store.kv_set("fec_ids", json.dumps(cache, separators=(",", ":")))
        return found

    def fec(self, c: dict, since: str, deadline: float | None = None) -> int:
        """Itemized receipts (schedule A) for each of the candidate's committees. Seek pagination (the API's
        last_indexes), newest first, stopping at a per-committee cursor: re-runs read only receipts filed since the
        last one (with a 60-day overlap for late itemizations and amendments)."""
        cid = self.fec_candidate_id(c)
        if not cid:
            return 0
        n = 0
        d = self._fec("/committees/", {"candidate_id": cid, "per_page": 20})
        for com in (d or {}).get("results", []):
            com_id = com["committee_id"]
            ck = f"fec:a:{com_id}"
            cursor = self.store.kv_get(ck)
            floor = since[:10]
            if cursor:
                floor = max(floor, (dt.date.fromisoformat(cursor) - dt.timedelta(days=60)).isoformat())
            params = {"committee_id": com_id, "per_page": 100, "sort": "-contribution_receipt_date", "min_date": floor}
            newest = cursor or ""
            for _ in range(200):
                if deadline and time.monotonic() > deadline:
                    break
                rr = self._fec("/schedules/schedule_a/", params)
                rows = (rr or {}).get("results", [])
                for s in rows:
                    date = (s.get("contribution_receipt_date") or "")[:10] or None
                    newest = max(newest, date or "")
                    row = {"name": s.get("contributor_name") or "", "amount": s.get("contribution_receipt_amount"), "date": date,
                           "employer": s.get("contributor_employer") or "", "occupation": s.get("contributor_occupation") or "",
                           "city": s.get("contributor_city") or "", "state": s.get("contributor_state") or "",
                           "kind": "contribution", "line": s.get("sub_id") or ""}
                    rec = contribution_record(f"subj:{c['id']}", s.get("pdf_url") or f"{FEC}/schedules/schedule_a/?committee_id={com_id}",
                                              row, c["name"], "candidate", "FEC", target=c.get("target", ""),
                                              channel="api")
                    rec.meta.update({"subject_id": c["id"], "committee": com_id,
                                     "committee_name": com.get("name"), "contributor_committee_id": s.get("contributor_id")
                                     if (s.get("entity_type") or "") in ("PAC", "COM", "PTY", "ORG") else None})
                    if self.store.add_new(rec):
                        n += 1
                last = ((rr or {}).get("pagination") or {}).get("last_indexes") or {}
                if len(rows) < 100 or not last:
                    break
                params = {**params, **last}   # next page: seek after the last row returned
            else:
                pass
            if newest:
                self.store.kv_set(ck, newest)
        return n

    # ------------------------------------------------------------------ 4. entities over everything in the store
    def resolve_all(self, candidates: list[dict]) -> dict:
        """Give every donor, recipient and organization an entity id (meta.donor_entity / recipient_entity)."""
        for c in candidates:
            self.entities.add_subject(c)
        n = 0
        for r in self.store.iter_query("kind='contribution' AND meta NOT LIKE '%\"donor_entity\"%'"):
            m = dict(r["meta"])
            name = m.get("name") or ""
            kind = "org" if m.get("donor_is_org") else "person"
            cand_name = committee_candidate(name) if kind == "org" else None
            m["donor_entity"] = self.entities.resolve(name, kind, {"employer": m.get("employer"), "city": m.get("city"),
                                                                   "state": m.get("state"), "occupation": m.get("occupation")},
                                                      url=r["url"])
            if cand_name:
                m["committee_of"] = self.entities.resolve(cand_name, "person", url=r["url"])
            rcp = m.get("recipient") or ""
            if m.get("subject_id"):
                m["recipient_entity"] = self.entities.by_subject(m["subject_id"])
            elif rcp:
                m["recipient_entity"] = self.entities.resolve(rcp, "committee" if is_org(rcp) else "person", url=r["url"])
            if m.get("employer") and not m.get("donor_is_org"):
                m["employer_entity"] = self.entities.resolve(m["employer"], "org", url=r["url"])
            self.store.add(Record(r["source_id"], r["kind"], r["url"], title=r.get("title") or "", text=r.get("text") or "",
                                  published=r.get("published"), channel=r.get("channel") or "", target=r.get("target") or "",
                                  evidence=r.get("evidence") or "", meta=m, key=r.get("key")))
            n += 1
        self.entities.flush()
        return {"resolved": n, "entities": len(self.entities.entities)}

    # ------------------------------------------------------------------ 5. what the organizations are
    def org_queue(self, limit: int = 50, recheck_days: int = 90) -> list[str]:
        """Organization entities not yet looked up (or stale), biggest money first."""
        totals: dict[str, float] = {}
        weight = {"donor_entity": 1.0, "recipient_entity": 1.0, "employer_entity": 0.25}   # who gave/received comes first
        for r in self.store.iter_query("kind='contribution'"):
            m = r["meta"]
            for key, w in weight.items():
                eid = m.get(key)
                if eid and self.entities.entities.get(eid, {}).get("kind") in ("org", "committee"):
                    totals[eid] = totals.get(eid, 0) + w * abs(m.get("amount") or 0)
        cutoff = (dt.date.today() - dt.timedelta(days=recheck_days)).isoformat()
        due = [eid for eid in totals if (self.org_state.get(eid) or {}).get("checked", "") < cutoff]
        due.sort(key=lambda e: -totals[e])
        return due[:limit]

    def enrich(self, eid: str) -> dict:
        """Look an organization up everywhere available; write an `organization` record; return it."""
        e = self.entities.entities[eid]
        name = e["name"]
        info = {"entity_id": eid, "name": name, "aliases": e.get("aliases", []), "org_kind": "committee" if e["kind"] == "committee" else "",
                "what": [], "officers": [], "treasurer": "", "connected_org": "", "website": "", "ein": "", "fec_id": "",
                "city": "", "state": "", "recipients": [], "sources": [], "notes": []}
        hand = self.orgs_cfg.get(name.lower()) or next((o for a, o in self.orgs_cfg.items() if a in [x.lower() for x in e.get("aliases", [])]), None)
        if hand:
            info["website"] = hand.get("website") or ""
            info["org_kind"] = hand.get("kind") or info["org_kind"]
            if hand.get("what"):
                info["what"].append({"text": hand["what"], "src": hand.get("src") or "config/organizations.yaml", "basis": "hand note"})
            for o in hand.get("officers") or []:
                info["officers"].append({"name": o.get("name"), "role": o.get("role", ""), "src": o.get("src") or hand.get("src") or "config/organizations.yaml"})
        from .entities import PAC_WORDS

        if e["kind"] == "committee" or PAC_WORDS.search(name):
            self._enrich_fec(info)                  # only committees are registered with the FEC
        if not re.search(r"\b(llc|l\.l\.c|llp|lllp|pllc|p\.c|pc|lp)\b\.?", name, re.I):
            self._enrich_propublica(info)           # for-profit legal forms are never IRS-exempt organizations
        self._enrich_opencorporates(info)
        if info["website"]:
            self._enrich_site(info)
        kind = info["org_kind"] or ("committee" if info["fec_id"] else "nonprofit" if info["ein"] else "company" if any(
            s.get("registry") == "opencorporates" for s in info["sources"]) else "unknown")
        info["org_kind"] = kind
        summary = "; ".join(w["text"] for w in info["what"][:4])
        rec = Record("org", "organization", info["sources"][0]["u"] if info["sources"] else f"entity:{eid}",
                     title=name, text=summary or name, channel="api" if info["sources"] else "derived", evidence="primary",
                     key=f"org|{eid}", meta={**info, "opened": bool(info["sources"]), "opened_url": info["sources"][0]["u"] if info["sources"] else ""})
        self.store.add(rec)
        self.org_state[eid] = {"checked": dt.date.today().isoformat(), "kind": kind, "sources": len(info["sources"])}
        e["attrs"]["org_kind"] = kind
        return info

    def _enrich_fec(self, info: dict):
        name = info["name"]
        d = self._fec("/committees/", {"q": name, "per_page": 5})
        from .entities import norm_org

        hit = next((r for r in (d or {}).get("results", []) if norm_org(r.get("name") or "") == norm_org(name)), None)
        if not hit:
            return
        cid = hit["committee_id"]
        det = (self._fec(f"/committee/{cid}/") or {}).get("results", [{}])
        det = det[0] if det else {}
        src = f"{FEC}/committee/{cid}/"
        info["fec_id"] = cid
        info["sources"].append({"t": f"FEC committee {cid}", "u": src, "registry": "fec"})
        what = ", ".join(x for x in (det.get("committee_type_full"), det.get("designation_full"), det.get("organization_type_full"),
                                     det.get("party_full")) if x)
        if what:
            info["what"].append({"text": f"Registered with the FEC as a {what}", "src": src, "basis": "FEC registration"})
        if det.get("affiliated_committee_name"):
            info["what"].append({"text": f"Affiliated committee: {det['affiliated_committee_name']}", "src": src, "basis": "FEC registration"})
        info["connected_org"] = det.get("connected_organization_name") or ""
        info["treasurer"] = det.get("treasurer_name") or ""
        if info["treasurer"]:
            info["officers"].append({"name": info["treasurer"], "role": "treasurer", "src": src})
        if det.get("custodian_of_records_name"):
            info["officers"].append({"name": det["custodian_of_records_name"], "role": "custodian of records", "src": src})
        info["city"], info["state"] = det.get("city") or "", det.get("state") or ""
        info["website"] = info["website"] or (det.get("website") or "")
        # who receives their money (schedule B: disbursements to candidates/committees)
        rr = self._fec("/schedules/schedule_b/", {"committee_id": cid, "per_page": 100, "sort": "-disbursement_date",
                                                 **({"recipient_state": self.state} if self.state else {})})
        for s in (rr or {}).get("results", [])[:100]:
            info["recipients"].append({"name": s.get("recipient_name"), "amount": s.get("disbursement_amount"),
                                       "date": (s.get("disbursement_date") or "")[:10], "purpose": s.get("disbursement_description"),
                                       "src": s.get("pdf_url") or f"{FEC}/schedules/schedule_b/?committee_id={cid}"})

    def _enrich_propublica(self, info: dict, name: str | None = None, label: str = ""):
        """IRS exempt-organization data for the org itself, or (label set) for its connected organization."""
        if not self.f:
            return
        from .entities import norm_org

        name = name or info["name"]
        try:
            r = self.f.get(f"{PROPUBLICA}/search.json", params={"q": name, **({"state[id]": self.state} if self.state else {})},
                           use_cache=False)
        except (Blocked, Disallowed):
            return
        if r.status != 200:
            return
        try:
            orgs = r.json().get("organizations", [])
        except Exception:  # noqa: BLE001
            return
        hit = next((o for o in orgs if norm_org(o.get("name") or "") == norm_org(name)), None)
        if not hit and orgs and norm_org(orgs[0].get("name") or "").startswith(norm_org(name)):
            hit = orgs[0]
        if not hit:
            if not label and info.get("connected_org") and norm_org(info["connected_org"]) != norm_org(name):
                self._enrich_propublica(info, info["connected_org"], label=f"Connected organization {info['connected_org']}: ")
            return
        ein = hit.get("ein")
        src = f"{PROPUBLICA}/organizations/{ein}.json"
        if not label:
            info["ein"] = str(ein)
        info["sources"].append({"t": f"ProPublica Nonprofit Explorer (IRS data) EIN {ein}", "u": src, "registry": "propublica"})
        sub = str(hit.get("subseccd") or hit.get("subsection_code") or "")
        ntee = (hit.get("ntee_code") or "")[:1].upper()
        parts = []
        if sub in SUBSECTION:
            parts.append(SUBSECTION[sub])
        if ntee in NTEE:
            parts.append(f"activity area: {NTEE[ntee]} (NTEE {hit.get('ntee_code')})")
        if parts:
            info["what"].append({"text": label + "IRS-registered nonprofit: " + "; ".join(parts), "src": src,
                                 "basis": "IRS Business Master File via ProPublica"})
        if not label:
            info["city"] = info["city"] or (hit.get("city") or "")
            info["state"] = info["state"] or (hit.get("state") or "")
        try:
            rr = self.f.get(src, use_cache=False)
            if rr.status == 200:
                filings = rr.json().get("filings_with_data", [])
                if filings:
                    f0 = filings[0]
                    info["what"].append({"text": label + f"Most recent IRS filing (tax year {f0.get('tax_prd_yr')}): revenue "
                                                 f"${(f0.get('totrevenue') or 0):,.0f}, expenses ${(f0.get('totfuncexpns') or 0):,.0f}",
                                         "src": f0.get("pdf_url") or src, "basis": "Form 990"})
        except (Blocked, Disallowed):
            pass
        except Exception:  # noqa: BLE001
            pass

    def _enrich_opencorporates(self, info: dict):
        key = os.environ.get("OPENCORPORATES_API_KEY")
        if not key or not self.f:
            return
        from .entities import norm_org

        try:
            juris = f"us_{self.state.lower()}" if self.state else ""
            r = self.f.get(f"{OPENCORPORATES}/companies/search", params={"q": info["name"], "api_token": key, "per_page": 5,
                                                                         **({"jurisdiction_code": juris} if juris else {})},
                           use_cache=False)
        except (Blocked, Disallowed):
            return
        if r.status != 200:
            return
        try:
            comps = [c["company"] for c in r.json()["results"]["companies"]]
        except Exception:  # noqa: BLE001
            return
        hit = next((c for c in comps if norm_org(c.get("name") or "") == norm_org(info["name"])), None)
        if not hit:
            return
        jc = hit.get("jurisdiction_code") or juris or "us"
        src = hit.get("opencorporates_url") or f"{OPENCORPORATES}/companies/{jc}/{hit.get('company_number')}"
        where = (self.state or jc.split("_")[-1].upper())
        info["sources"].append({"t": f"{where} corporate registry via OpenCorporates ({hit.get('company_number')})", "u": src,
                                "registry": "opencorporates"})
        info["what"].append({"text": f"{where}-registered {hit.get('company_type') or 'company'} ({hit.get('current_status') or 'status unknown'}),"
                                     f" formed {hit.get('incorporation_date') or '?'}; registered address {hit.get('registered_address_in_full') or '?'}",
                             "src": src, "basis": "corporate registry"})
        try:
            rr = self.f.get(f"{OPENCORPORATES}/companies/{jc}/{hit.get('company_number')}", params={"api_token": key}, use_cache=False)
            if rr.status == 200:
                for o in rr.json()["results"]["company"].get("officers", []):
                    oo = o.get("officer", o)
                    info["officers"].append({"name": oo.get("name"), "role": oo.get("position", ""), "src": src})
        except (Blocked, Disallowed):
            pass
        except Exception:  # noqa: BLE001
            pass

    def _enrich_site(self, info: dict):
        """The organization's own 'about' page: what it says it does and any listed officers/board."""
        if not self.f:
            return
        from .. import extract

        try:
            r = self.f.get(info["website"])
        except (Blocked, Disallowed):
            info["notes"].append("site not crawlable")
            return
        except Exception:  # noqa: BLE001
            return
        if r.status != 200 or "html" not in (r.content_type or ""):
            return
        soup = extract.soup_of(r.text)
        about_url = None
        for a in soup.find_all("a", href=True):
            t = (a.get_text(" ", strip=True) or "").lower()
            if re.search(r"\babout\b|mission|who we are|leadership|board|our team|staff|officers", t):
                from urllib.parse import urljoin

                about_url = urljoin(r.url, a["href"])
                break
        page = r
        if about_url and about_url != r.url:
            try:
                page = self.f.get(about_url)
            except Exception:  # noqa: BLE001
                page = r
        text = extract.main_text(page.text) if page.status == 200 else ""
        if text:
            info["sources"].append({"t": f"{info['name']} — about page", "u": page.url, "registry": "site"})
            info["what"].append({"text": re.sub(r"\s+", " ", text)[:700], "src": page.url, "basis": "self-description"})
            for m in re.finditer(r"([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){1,3})\s*[,–\-]\s*((?:Executive\s+)?(?:President|Chair(?:man|woman|person)?|"
                                 r"Vice\s+Chair|Treasurer|Secretary|Director|CEO|Founder|Board\s+Member|Trustee)[^,.\n]{0,40})", text):
                info["officers"].append({"name": m.group(1), "role": m.group(2).strip(), "src": page.url})
            for m in re.finditer(r"((?:Executive\s+)?(?:President|Chair(?:man|woman|person)?|Treasurer|Secretary|CEO|Founder))\s*[:–\-]\s*"
                                 r"([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){1,3})", text):
                info["officers"].append({"name": m.group(2), "role": m.group(1).strip(), "src": page.url})
        seen = set()
        info["officers"] = [o for o in info["officers"] if o.get("name") and not (o["name"].lower() in seen or seen.add(o["name"].lower()))]

    # ------------------------------------------------------------------ 6. the commission's portal (confirmed URLs only)
    def probe_commission(self) -> dict:
        """Open the state system's home page and list the search/report links it offers, for a person to confirm."""
        gc = self.opts.get("state_commission") or {}
        home = gc.get("home")
        out = {"home": home, "links": [], "checked": dt.date.today().isoformat()}
        if not home or not self.f:
            return out
        from .. import extract
        from urllib.parse import urljoin

        try:
            r = self.f.get(home)
        except (Blocked, Disallowed) as exc:
            out["error"] = str(exc)
            return out
        if r.status == 200 and "html" in (r.content_type or ""):
            soup = extract.soup_of(r.text)
            for a in soup.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                h = urljoin(r.url, a["href"])
                if re.search(r"search|efile|e-file|report|view|campaign|disclosure|lobby|data", f"{t} {h}", re.I):
                    out["links"].append({"text": t[:80], "url": h})
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "finance_portal.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        return out

    def commission_search(self, c: dict) -> int:
        """Only with a person-confirmed search_url template in config/finance.yaml (placeholders {last} {first})."""
        gc = self.opts.get("state_commission") or {}
        tpl = gc.get("search_url")
        if not tpl or not gc.get("confirmed") or not self.f:
            return 0
        parts = c["name"].split()
        url = tpl.format(last=parts[-1], first=parts[0], name=c["name"])
        try:
            r = self.f.get(url)
        except (Blocked, Disallowed):
            return 0
        if r.status != 200:
            return 0
        text = r.text if "html" in (r.content_type or "") else ""
        if not text:
            return 0
        from .. import extract

        rows = parse_ccdr_text(extract.main_text(text))
        n = 0
        for row in rows:
            rec = contribution_record(f"subj:{c['id']}", r.url, row, c["name"], "candidate",
                                      self.commission_name, needs_review=True, target=c.get("target", ""), channel="html")
            rec.meta["subject_id"] = c["id"]
            if self.store.add_new(rec):
                n += 1
        return n

    # ------------------------------------------------------------------ run
    def run(self, candidates: list[dict], since: str = "2016-01-01", deadline: float | None = None,
            org_limit: int = 40) -> dict:
        out = {"imported": self.from_imports(candidates)}
        out["documents"] = self.from_documents(candidates)
        fec_n = 0
        if self.f:
            for c in candidates:
                if deadline and time.monotonic() > deadline:
                    break
                if c.get("fec_id") or re.search(r"u\.?s\.? (house|senate)|congress", (c.get("office") or "").lower()):
                    try:
                        fec_n += self.fec(c, since, deadline)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  fec {c['id']}: {type(exc).__name__}: {exc}"[:200])
                if (self.opts.get("state_commission") or {}).get("confirmed"):
                    try:
                        out["commission"] = out.get("commission", 0) + self.commission_search(c)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  commission {c['id']}: {type(exc).__name__}: {exc}"[:200])
        out["fec"] = fec_n
        out.update(self.resolve_all(candidates))
        enriched = 0
        for eid in self.org_queue(limit=org_limit):
            if deadline and time.monotonic() > deadline:
                break
            try:
                self.enrich(eid)
                enriched += 1
            except Exception as exc:  # noqa: BLE001
                self.log(f"  org {eid}: {type(exc).__name__}: {exc}"[:200])
        out["organizations"] = enriched
        self.flush()
        return out
