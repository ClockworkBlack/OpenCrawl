"""The connections graph: donors, organizations, candidates, offices and the patterns between them.

Nodes are entities (opencrawl.entities): person, candidate, org, committee, plus bodies
(a council, a board) and matters (an ordinance) where an edge needs one.
Edges are observations with the records they rest on:
  contributed   donor -> candidate/committee   amount, date, kind (contribution / in_kind / loan)
  paid          committee -> payee             expenditure
  funds         org/committee -> recipient     what a PAC gives (FEC schedule B / its own filings)
  officer_of    person -> org                  role (treasurer, president, board member ...)
  employed_by   person -> org                  from the donor's listed employer
  committee_of  committee -> candidate         "Friends of X" is X's committee
  connected_to  committee -> org               FEC connected organization
  endorsed      org/person -> candidate
  serves        person -> body                 office records (Legistar, officials listings)
  sponsored     person -> matter               Legistar sponsors
  appointed     official -> person (body)      appointment announcements
  member_of     person -> org                  stated memberships / caucuses

Patterns (every one lists the record urls it rests on; none is a conclusion):
  shared_donor      one donor gave to two or more candidates
  employer_cluster  three or more donors to one candidate list the same employer
  org_chain         money moved org -> org -> candidate
  officer_overlap   one person is an officer of two or more organizations in the graph,
                    or an officer of an organization that gave to a candidate they sit with
  donor_official    a donor (or their employer) is itself a candidate or office-holder
  donor_matter      a donor's or employer's name appears in a matter the recipient voted on
                    within 180 days of the gift  -> evidence "lead": verify before citing
  shared_endorser   one organization endorsed two or more candidates
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

from .entities import EntityIndex, committee_candidate, is_org, norm_org
from ..store import Store

MATTER_WINDOW_DAYS = 180


def _d(s: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


class Graph:
    def __init__(self, entities: EntityIndex):
        self.ix = entities
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[str] = set()
        self.out: dict[str, list[dict]] = defaultdict(list)      # node -> edges leaving it
        self.inn: dict[str, list[dict]] = defaultdict(list)      # node -> edges arriving at it
        self.pattern_index: dict[str, list[int]] = {}            # node -> indexes into the patterns list

    def node(self, eid: str, kind: str | None = None, name: str | None = None, **attrs) -> str:
        if eid not in self.nodes:
            e = self.ix.get(eid) if eid in self.ix.entities else None
            self.nodes[eid] = {"id": eid, "kind": kind or (e or {}).get("kind") or "other",
                               "name": name or (e or {}).get("name") or eid, "attrs": {}}
        if attrs:
            self.nodes[eid]["attrs"].update({k: v for k, v in attrs.items() if v not in (None, "")})
        return eid

    def edge(self, a: str, b: str, etype: str, src: str, **attrs) -> dict | None:
        if not a or not b or a == b:
            return None
        key = f"{a}|{b}|{etype}|{attrs.get('date') or ''}|{attrs.get('amount') or ''}|{attrs.get('role') or ''}|{src}"
        if key in self._edge_keys:
            return None
        self._edge_keys.add(key)
        e = {"a": a, "b": b, "type": etype, "src": src, **{k: v for k, v in attrs.items() if v not in (None, "")}}
        self.edges.append(e)
        self.out[a].append(e)
        self.inn[b].append(e)
        return e

    def neighbors(self, eid: str, etype: str | None = None, out: bool = True):
        for e in (self.out if out else self.inn).get(eid, ()):
            if etype is None or e["type"] == etype:
                yield e

    def index_patterns(self, pats: list[dict]) -> None:
        """Which patterns mention each node (so a candidate's patterns are a lookup, not a scan of every pattern)."""
        idx: dict[str, list[int]] = defaultdict(list)

        def ids(x, acc):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("id", "donor", "recipient", "person", "endorser") and isinstance(v, str):
                        acc.add(v)
                    else:
                        ids(v, acc)
            elif isinstance(x, list):
                for v in x:
                    ids(v, acc)
        for i, p in enumerate(pats):
            acc: set = set()
            ids(p, acc)
            for n in acc:
                idx[n].append(i)
        self.pattern_index = dict(idx)


def build(store: Store, entities: EntityIndex, targets: dict | None = None) -> Graph:
    g = Graph(entities)
    # money
    for r in store.iter_query("kind='contribution'"):
        m = r["meta"]
        donor, rcp = m.get("donor_entity"), m.get("recipient_entity")
        if not donor:
            continue
        g.node(donor)
        if rcp:
            g.node(rcp)
        if m.get("type") == "expenditure":
            if rcp:
                g.edge(rcp, donor, "paid", r["url"], amount=m.get("amount"), date=m.get("date"), purpose=m.get("purpose"),
                       evidence="primary", filing=m.get("filing_source"), review=bool(m.get("needs_review")))
        elif rcp:
            g.edge(donor, rcp, "contributed", r["url"], amount=m.get("amount"), date=m.get("date"), kind=m.get("type"),
                   evidence="primary", filing=m.get("filing_source"), review=bool(m.get("needs_review")),
                   employer=m.get("employer"), occupation=m.get("occupation"))
        if m.get("employer_entity"):
            g.node(m["employer_entity"])
            g.edge(donor, m["employer_entity"], "employed_by", r["url"], evidence="primary")
        if m.get("committee_of"):
            g.node(m["committee_of"])
            g.edge(donor, m["committee_of"], "committee_of", r["url"], evidence="primary")
    # organizations: officers, connected orgs, what they fund
    for r in store.iter_query("kind='organization'"):
        m = r["meta"]
        oid = m.get("entity_id")
        if not oid:
            continue
        g.node(oid, what="; ".join(w.get("text", "") for w in (m.get("what") or [])[:2])[:300], org_kind=m.get("org_kind"),
               website=m.get("website"), ein=m.get("ein"), fec_id=m.get("fec_id"))
        for o in m.get("officers") or []:
            if not o.get("name"):
                continue
            pid = entities.resolve(o["name"], "person", url=o.get("src"))
            if pid:
                g.node(pid)
                g.edge(pid, oid, "officer_of", o.get("src") or r["url"], role=o.get("role", ""), evidence="primary")
        if m.get("connected_org"):
            cid = entities.resolve(m["connected_org"], "org", url=r["url"])
            if cid:
                g.node(cid)
                g.edge(oid, cid, "connected_to", r["url"], evidence="primary")
        for rc in m.get("recipients") or []:
            if not rc.get("name"):
                continue
            kind = "committee" if is_org(rc["name"]) else "person"
            rid = entities.resolve(rc["name"], kind, url=rc.get("src"))
            if rid:
                g.node(rid)
                g.edge(oid, rid, "funds", rc.get("src") or r["url"], amount=rc.get("amount"), date=rc.get("date"),
                       purpose=rc.get("purpose"), evidence="primary")
                cn = committee_candidate(rc["name"]) if kind == "committee" else None
                if cn:
                    pid = entities.resolve(cn, "person", url=rc.get("src"))
                    if pid:
                        g.node(pid)
                        g.edge(rid, pid, "committee_of", rc.get("src") or r["url"], evidence="primary")
    # endorsements (candidate research) and memberships
    for r in store.iter_query("kind='connection'"):
        m = r["meta"]
        cand = m.get("subject_id")
        ceid = entities.by_subject(cand) if cand else None
        if not ceid:
            continue
        g.node(ceid)
        if m.get("type") == "endorsement" and m.get("party"):
            who = m["party"]
            eid = entities.resolve(who, "org" if is_org(who) else "person", url=r["url"])
            if eid:
                g.node(eid)
                g.edge(eid, ceid, "endorsed", r["url"], date=(r.get("published") or "")[:10],
                       evidence=r.get("evidence") or "primary", self_reported=bool(m.get("self_reported")))
        elif m.get("type") in ("membership", "affiliation") and m.get("party"):
            eid = entities.resolve(m["party"], "org", url=r["url"])
            if eid:
                g.node(eid)
                g.edge(ceid, eid, "member_of", r["url"], role=m.get("role", ""), evidence=r.get("evidence") or "primary")
    # offices held (Legistar office records) and sponsors
    for r in store.iter_query("kind='official'"):
        m = r["meta"]
        name = r.get("title") or m.get("name") or ""
        body = m.get("OfficeRecordBodyName") or m.get("body") or ""
        if not name or not body:
            continue
        pid = entities.resolve(name, "person", url=r["url"])
        if not pid:
            continue
        bid = f"body:{r.get('target') or r['source_id'].split(':')[0]}:{norm_org(body)}"
        g.node(pid)
        g.node(bid, "body", body, target=r.get("target"))
        g.edge(pid, bid, "serves", r["url"], role=m.get("OfficeRecordTitle") or m.get("role", ""),
               start=(m.get("OfficeRecordStartDate") or m.get("start") or "")[:10],
               end=(m.get("OfficeRecordEndDate") or m.get("end") or "")[:10], evidence="primary")
    for r in store.iter_query("kind IN ('ordinance','matter') AND meta LIKE '%\"sponsors\": [{%'"):
        m = r["meta"]
        mid = f"matter:{r['source_id']}:{m.get('MatterId') or m.get('MatterFile')}"
        g.node(mid, "matter", (m.get("MatterFile") or "") + " " + (r.get("title") or "")[:120], target=r.get("target"),
               status=m.get("MatterStatusName"))
        for sp in m.get("sponsors") or []:
            if sp.get("name"):
                pid = entities.resolve(sp["name"], "person", url=m.get("opened_url") or r["url"])
                if pid:
                    g.node(pid)
                    g.edge(pid, mid, "sponsored", m.get("opened_url") or r["url"], date=(r.get("published") or "")[:10], evidence="primary")
    for r in store.iter_query("kind='appointment'"):
        m = r["meta"]
        if m.get("person") and m.get("body"):
            pid = entities.resolve(m["person"], "person", url=r["url"])
            bid = f"body:{m.get('level') or 'state'}:{norm_org(m['body'])}"
            if pid:
                g.node(pid)
                g.node(bid, "body", m["body"])
                g.edge(pid, bid, "appointed_to", r["url"], date=(r.get("published") or "")[:10], by=m.get("by", ""),
                       evidence=r.get("evidence") or "primary")
    return g


# ----------------------------------------------------------------------------- patterns
def patterns(g: Graph, store: Store | None = None) -> list[dict]:
    out: list[dict] = []
    contrib = [e for e in g.edges if e["type"] == "contributed"]
    by_donor: dict[str, list[dict]] = defaultdict(list)
    by_rcp: dict[str, list[dict]] = defaultdict(list)
    for e in contrib:
        by_donor[e["a"]].append(e)
        by_rcp[e["b"]].append(e)
    name = lambda i: g.nodes.get(i, {}).get("name", i)  # noqa: E731
    # shared donors
    for donor, es in by_donor.items():
        rcps = defaultdict(float)
        for e in es:
            rcps[e["b"]] += e.get("amount") or 0
        if len(rcps) >= 2:
            out.append({"kind": "shared_donor", "donor": donor, "donor_name": name(donor),
                        "recipients": [{"id": r, "name": name(r), "total": round(t, 2)} for r, t in sorted(rcps.items(), key=lambda kv: -kv[1])],
                        "total": round(sum(rcps.values()), 2), "n": len(es), "src": sorted({e["src"] for e in es})[:20],
                        "evidence": "primary", "note": "filings list the same donor giving to several candidates or committees"})
    # employer clusters
    for rcp, es in by_rcp.items():
        emp = defaultdict(list)
        for e in es:
            if e.get("employer"):
                emp[norm_org(e["employer"])].append(e)
        for k, ees in emp.items():
            donors = {e["a"] for e in ees}
            if len(donors) >= 3:
                out.append({"kind": "employer_cluster", "recipient": rcp, "recipient_name": name(rcp), "employer": ees[0]["employer"],
                            "donors": [{"id": d, "name": name(d)} for d in donors], "total": round(sum(e.get("amount") or 0 for e in ees), 2),
                            "src": sorted({e["src"] for e in ees})[:20], "evidence": "primary",
                            "note": "several donors to one recipient list the same employer"})
    # org chains: org -> org/committee -> candidate (contributed or funds edges)
    money = [e for e in g.edges if e["type"] in ("contributed", "funds")]
    gives: dict[str, list[dict]] = defaultdict(list)
    for e in money + [e for e in g.edges if e["type"] == "committee_of"]:
        gives[e["a"]].append(e)
    for e1 in money:
        mid = e1["b"]
        if g.nodes.get(e1["a"], {}).get("kind") in ("org", "committee") and g.nodes.get(mid, {}).get("kind") in ("org", "committee"):
            for e2 in gives.get(mid, []):
                if g.nodes.get(e2["b"], {}).get("kind") in ("subject", "person", "committee") and e2["b"] != e1["a"]:
                    out.append({"kind": "org_chain", "path": [{"id": e1["a"], "name": name(e1["a"])}, {"id": mid, "name": name(mid)},
                                                              {"id": e2["b"], "name": name(e2["b"])}],
                                "amounts": [e1.get("amount"), e2.get("amount")], "dates": [e1.get("date"), e2.get("date")],
                                "src": [e1["src"], e2["src"]], "evidence": "primary",
                                "note": "money recorded moving from one organization to another and on to a candidate"})
    # officer overlap and officers who are candidates/office-holders
    officer_of: dict[str, list[dict]] = defaultdict(list)
    for e in g.edges:
        if e["type"] == "officer_of":
            officer_of[e["a"]].append(e)
    for pid, es in officer_of.items():
        orgs = {e["b"] for e in es}
        pnode = g.nodes.get(pid, {})
        gave_to = set()
        for o in orgs:
            for e in gives.get(o, []):
                gave_to.add(e["b"])
        if len(orgs) >= 2 or pnode.get("kind") == "subject" or gave_to:
            out.append({"kind": "officer_overlap", "person": pid, "person_name": name(pid), "is_subject": pnode.get("kind") == "subject",
                        "organizations": [{"id": o, "name": name(o), "roles": [e.get("role", "") for e in es if e["b"] == o]} for o in orgs],
                        "organizations_gave_to": [{"id": r, "name": name(r)} for r in gave_to],
                        "src": sorted({e["src"] for e in es})[:20], "evidence": "primary",
                        "note": "one person listed as an officer of these organizations (registries / filings)"})
    # donors who are themselves candidates or office-holders
    serves = {e["a"] for e in g.edges if e["type"] in ("serves", "sponsored")}
    for donor, es in by_donor.items():
        k = g.nodes.get(donor, {}).get("kind")
        if k == "subject" or donor in serves:
            out.append({"kind": "donor_official", "donor": donor, "donor_name": name(donor), "is_subject": k == "subject",
                        "recipients": [{"id": e["b"], "name": name(e["b"]), "amount": e.get("amount"), "date": e.get("date")} for e in es],
                        "src": sorted({e["src"] for e in es})[:20], "evidence": "primary",
                        "note": "a candidate or office-holder appears as a donor"})
    # shared endorsers
    endo: dict[str, set] = defaultdict(set)
    endo_src: dict[str, set] = defaultdict(set)
    for e in g.edges:
        if e["type"] == "endorsed":
            endo[e["a"]].add(e["b"])
            endo_src[e["a"]].add(e["src"])
    for org, cands in endo.items():
        if len(cands) >= 2:
            out.append({"kind": "shared_endorser", "endorser": org, "endorser_name": name(org),
                        "candidates": [{"id": c, "name": name(c)} for c in cands], "src": sorted(endo_src[org])[:20],
                        "evidence": "primary", "note": "the same organization or person endorsed these candidates"})
    # donor names in matters the recipient voted on (lead: verify)
    if store is not None:
        out += donor_matter_leads(g, store, by_rcp)
    return out


def donor_matter_leads(g: Graph, store: Store, by_rcp: dict) -> list[dict]:
    """A donor's (or their employer's) name inside a matter the recipient voted on, within MATTER_WINDOW_DAYS.
    Each vote's text is normalized once and indexed by token, so a name is matched against the few votes that
    contain all its words instead of against every vote."""
    out = []
    norm_cache: dict[str, str] = {}

    def nrm(x: str) -> str:
        v = norm_cache.get(x)
        if v is None:
            v = norm_cache[x] = norm_org(x)
        return v

    for rcp, es in by_rcp.items():
        node = g.nodes.get(rcp, {})
        if node.get("kind") != "subject":
            continue
        cand_id = (g.ix.get(rcp) or {}).get("attrs", {}).get("subject_id")
        if not cand_id:
            continue
        votes = store.raw("source_id=? AND kind='vote'", (f"subj:{cand_id}",))   # meta decoded only for matches
        if not votes:
            continue
        vinfo, postings = [], defaultdict(set)
        for i, v in enumerate(votes):
            hay = nrm(f"{v['title'] or ''} {v['text'] or ''}")
            vinfo.append((_d(v["published"]), f" {hay} "))
            for tok in set(hay.split()):
                postings[tok].add(i)
        term_hits: dict[str, list[int]] = {}

        def hits_for(nt: str) -> list[int]:
            h = term_hits.get(nt)
            if h is None:
                cand = None
                for tok in sorted(nt.split(), key=lambda t: len(postings.get(t, ()))):
                    p = postings.get(tok)
                    cand = set(p) if (p and cand is None) else (cand & p if p else set())
                    if not cand:
                        break
                h = term_hits[nt] = sorted(i for i in (cand or ()) if f" {nt} " in vinfo[i][1])
            return h

        for e in es:
            donor_name = g.nodes.get(e["a"], {}).get("name", "")
            terms = [t for t in (donor_name, e.get("employer")) if t and len(nrm(t)) >= 6]
            if not terms:
                continue
            d0 = _d(e.get("date"))
            matched: set[int] = set()
            for t in terms:
                for i in hits_for(nrm(t)):
                    if i in matched:
                        continue
                    vd = vinfo[i][0]
                    if d0 and vd and abs((vd - d0).days) > MATTER_WINDOW_DAYS:
                        continue
                    matched.add(i)
                    v = votes[i]
                    out.append({"kind": "donor_matter", "donor": e["a"], "donor_name": donor_name, "term": t,
                                "recipient": rcp, "recipient_name": node.get("name"), "amount": e.get("amount"), "gift_date": e.get("date"),
                                "matter": v["title"], "vote": json.loads(v["meta"] or "{}").get("vote"), "vote_date": v["published"],
                                "days_apart": abs((vd - d0).days) if d0 and vd else None,
                                "src": [e["src"], v["url"]], "evidence": "lead",
                                "note": "a donor's or employer's name appears in a matter the recipient voted on; "
                                        "open both records and confirm it is the same entity before citing"})
    return out


# ----------------------------------------------------------------------------- per-candidate money summary
def money_for(g: Graph, store: Store, entities: EntityIndex, subject_id: str, pats: list[dict] | None = None) -> dict:
    eid = entities.by_subject(subject_id)
    if not eid:
        return {}
    ins = [e for e in g.inn.get(eid, ()) if e["type"] == "contributed"]
    outs = [e for e in g.out.get(eid, ()) if e["type"] == "paid"]
    by_donor: dict[str, dict] = {}
    for e in ins:
        d = by_donor.setdefault(e["a"], {"id": e["a"], "name": g.nodes[e["a"]]["name"], "kind": g.nodes[e["a"]]["kind"], "total": 0.0,
                                         "n": 0, "first": e.get("date"), "last": e.get("date"), "src": []})
        d["total"] += e.get("amount") or 0
        d["n"] += 1
        d["first"] = min(x for x in (d["first"], e.get("date")) if x) if (d["first"] or e.get("date")) else None
        d["last"] = max(x for x in (d["last"], e.get("date")) if x) if (d["last"] or e.get("date")) else None
        if e["src"] not in d["src"] and len(d["src"]) < 5:
            d["src"].append(e["src"])
        if e.get("employer"):
            d.setdefault("employer", e["employer"])
        if e.get("occupation"):
            d.setdefault("occupation", e["occupation"])
    orgs = []
    for d in by_donor.values():
        d["total"] = round(d["total"], 2)
        if d["kind"] in ("org", "committee"):
            n = g.nodes.get(d["id"], {}).get("attrs", {})
            d["what"] = n.get("what", "")
            d["org_kind"] = n.get("org_kind", "")
            d["officers"] = [{"name": g.nodes[e["a"]]["name"], "role": e.get("role", ""), "src": e["src"]}
                             for e in g.inn.get(d["id"], ()) if e["type"] == "officer_of"][:12]
            d["also_funds"] = [{"name": g.nodes[e["b"]]["name"], "amount": e.get("amount"), "src": e["src"]}
                               for e in g.out.get(d["id"], ()) if e["type"] in ("funds", "contributed") and e["b"] != eid][:12]
            orgs.append(d)
    employers = defaultdict(float)
    for e in ins:
        if e.get("employer"):
            employers[e["employer"]] += e.get("amount") or 0
    if pats and not g.pattern_index:
        g.index_patterns(pats)
    related = [pats[i] for i in g.pattern_index.get(eid, [])] if pats else []
    return {"entity": eid, "total_raised": round(sum(e.get("amount") or 0 for e in ins), 2), "contributions": len(ins),
            "total_spent": round(sum(e.get("amount") or 0 for e in outs), 2), "expenditures": len(outs),
            "donors": sorted(by_donor.values(), key=lambda d: -d["total"])[:60],
            "organizations": sorted(orgs, key=lambda d: -d["total"])[:40],
            "employers": [{"employer": k, "total": round(v, 2)} for k, v in sorted(employers.items(), key=lambda kv: -kv[1])[:20]],
            "patterns": related[:40], "sources": sorted({e["src"] for e in ins + outs})[:200]}


def export_graph(g: Graph, pats: list[dict], out: Path) -> dict:
    nodes = list(g.nodes.values())
    doc = {"note": "Observations from filings and registries, each with the records it rests on. Patterns are starting "
                   "points for a researcher, not findings; items with evidence 'lead' must be verified before use.",
           "counts": {"nodes": len(nodes), "edges": len(g.edges), "patterns": len(pats)},
           "nodes": nodes, "edges": g.edges, "patterns": pats}
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc["counts"]
