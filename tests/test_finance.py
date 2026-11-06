"""Campaign finance, organizations and the connections graph. Local stubs for the FEC and ProPublica APIs;
fictional people and organizations throughout."""
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from opencrawl.research import connections, finance
from opencrawl.research.entities import EntityIndex
from opencrawl.fetch import Fetcher
from opencrawl.names import name_variants
from opencrawl.store import Record, Store

REPORT = """CAMPAIGN CONTRIBUTION DISCLOSURE REPORT
Candidate Name: Robin Ashby
Office Sought: City Council Post 2
Report Type: October 25 Report
Reporting Period: 07/01/2026 - 09/30/2026

Contributions Received
Name                         Address                      Occupation / Employer        Date        Amount     Election
John A. Smith                123 Main St, Rifle, CO 30114  Attorney / Smith Law       08/14/2026  $500.00    General
Mountain Realtors PAC         3200 Presidential Dr, Denver, CO 30340                   09/02/2026  $1,000.00  General
Acme Development, LLC        PO Box 9, Rifle, CO 30114                                09/10/2026  $2,500.00  General
Mary Jones                   5 Elm St, Rifle, CO 30114     Engineer / Acme Development  09/11/2026  $250.00  General
Bob Brown                    7 Elm St, Rifle, CO 30114     Manager / Acme Development   09/11/2026  $250.00  General
Sue Green                    9 Elm St, Rifle, CO 30114     Clerk / Acme Development     09/12/2026  $250.00  General
Total Contributions                                                                                $4,750.00

Expenditures
Payee                        Address                      Purpose                      Date        Amount
Garfield Tribune             200 Main St, Rifle, CO      Advertising                  09/12/2026  $750.00

In-Kind Contributions
Contributor Name: Taylor Calder
Address: 22 Oak Ave, Rifle, CO 30114
Occupation: Teacher   Employer: Garfield County Schools
Date: 09/20/2026   Amount: $300.00   Description: catering for fundraiser
"""

CSV_BULK = """Filer,Contributor Name,Amount,Date,Employer,Occupation,City,State,Type
Morgan Okafor for Mayor,"SMITH, JOHN A",250.00,2026-08-20,Smith Law,Attorney,Rifle,CO,Contribution
Morgan Okafor for Mayor,Mountain Realtors PAC,1000.00,2026-09-03,,,Denver,CO,Contribution
"""

FEC_ROUTES = {}


def fec_handler(path, qs):
    if path.endswith("/candidates/search/"):
        return {"results": [{"name": "ASHBY, ROBIN", "candidate_id": "H0CO09999"}]}
    if path.endswith("/committees/"):
        if qs.get("candidate_id"):
            return {"results": [{"committee_id": "C0001", "name": "ROBIN ASHBY FOR CONGRESS"}]}
        q = (qs.get("q") or [""])[0].lower()
        if "realtors" in q:
            return {"results": [{"committee_id": "C0002", "name": "MOUNTAIN REALTORS PAC"}]}
        return {"results": []}
    if path.endswith("/committee/C0002/"):
        return {"results": [{"committee_id": "C0002", "name": "MOUNTAIN REALTORS PAC", "committee_type_full": "PAC - Nonqualified",
                             "designation_full": "Unauthorized", "organization_type_full": "Trade Association",
                             "connected_organization_name": "MOUNTAIN ASSOCIATION OF REALTORS", "treasurer_name": "TAYLOR CALDER",
                             "city": "DENVER", "state": "CO", "website": ""}]}
    if path.endswith("/schedules/schedule_a/"):
        FEC_ROUTES.setdefault("schedule_a", []).append(qs)
        if qs.get("last_index"):
            return {"results": []}
        return {"results": [{"contributor_name": "SMITH, JOHN A", "contribution_receipt_amount": 1000.0,
                             "contribution_receipt_date": "2026-06-01", "contributor_employer": "SMITH LAW",
                             "contributor_occupation": "ATTORNEY", "contributor_city": "CANTON", "contributor_state": "CO",
                             "sub_id": "4001", "pdf_url": "https://docquery.fec.gov/cgi-bin/fecimg/?4001", "entity_type": "IND"}],
                "pagination": {"last_indexes": {"last_index": "4001", "last_contribution_receipt_date": "2026-06-01"}}}
    if path.endswith("/schedules/schedule_b/"):
        return {"results": [{"recipient_name": "FRIENDS OF MORGAN OKAFOR", "disbursement_amount": 2000.0, "disbursement_date": "2026-05-05",
                             "disbursement_description": "CONTRIBUTION", "pdf_url": "https://docquery.fec.gov/cgi-bin/fecimg/?5001"}]}
    return None


def propublica_handler(path, qs):
    if path.endswith("/search.json"):
        q = (qs.get("q") or [""])[0].lower()
        if "realtors" in q:
            return {"organizations": [{"ein": 580000001, "name": "MOUNTAIN ASSOCIATION OF REALTORS", "city": "DENVER", "state": "CO",
                                       "ntee_code": "S41", "subseccd": 6}]}
        return {"organizations": []}
    if path.endswith("/organizations/580000001.json"):
        return {"organization": {"ein": 580000001}, "filings_with_data": [{"tax_prd_yr": 2024, "totrevenue": 5000000, "totfuncexpns": 4500000,
                                                                          "pdf_url": "https://projects.propublica.org/nonprofits/display_990/580000001/x"}]}
    return None


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlsplit(self.path)
        qs = parse_qs(u.query)
        if u.path == "/robots.txt":
            body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
        else:
            data = fec_handler(u.path, qs) if u.path.startswith("/fec") else propublica_handler(u.path, qs)
            if data is None:
                self.send_response(404); self.end_headers(); return
            body, ct = json.dumps(data).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def cand(cid, name, office="City Council Post 2", jur="rifle", **kw):
    c = {"id": cid, "name": name, "aliases": [], "websites": [], "social": [], "targets": [jur], "target": jur,
         "office": office, **kw}
    c["variants"] = name_variants(name)
    return c


def test_parse_disclosure_report_and_csv(tmp_path):
    rows = finance.parse_disclosure_text(REPORT)
    by = {r["name"]: r for r in rows}
    assert by["John A. Smith"]["amount"] == 500.0 and by["John A. Smith"]["employer"] == "Smith Law" and by["John A. Smith"]["date"] == "2026-08-14"
    assert by["Mountain Realtors PAC"]["amount"] == 1000.0 and by["Mountain Realtors PAC"]["city"] == "Denver"
    assert by["Garfield Tribune"]["kind"] == "expenditure" and by["Garfield Tribune"]["purpose"] == "Advertising"
    assert by["Taylor Calder"]["kind"] == "in_kind" and by["Taylor Calder"]["employer"] == "Garfield County Schools" and by["Taylor Calder"]["amount"] == 300.0
    assert "Total Contributions" not in by and "Reporting Period:" not in by
    assert finance.header_of(REPORT)["candidate"] == "Robin Ashby"
    p = tmp_path / "bulk.csv"
    p.write_text(CSV_BULK)
    rows = finance.parse_finance_csv(p)
    assert rows[0]["recipient"] == "Morgan Okafor for Mayor" and rows[0]["amount"] == 250.0 and rows[0]["date"] == "2026-08-20"


def test_finance_pipeline_and_connections(server, fetcher, tmp_path, monkeypatch):
    monkeypatch.setattr(finance, "FEC", server + "/fec/v1")
    monkeypatch.setattr(finance, "PROPUBLICA", server + "/pp/api/v2")
    monkeypatch.delenv("OPENCORPORATES_API_KEY", raising=False)
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "imports" / "finance").mkdir(parents=True)
    (root / "imports" / "finance" / "bulk.csv").write_text(CSV_BULK)
    for f in ("finance.yaml", "organizations.yaml"):
        (root / "config" / f).write_text((Path(__file__).resolve().parent.parent / "config" / f).read_text())
    store = Store(root / "data")
    # a city posted Robin Ashby's disclosure report; Robin Ashby also ran for Congress (FEC); Morgan Okafor is mayor and got money too
    store.add(Record("canton:site", "document", "https://canton.example/finance/ashby-oct.pdf", title="Campaign Contribution Disclosure Report - Ashby",
                     text=REPORT, target="rifle", channel="pdf", evidence="primary", meta={"doc_type": "finance_report"}))
    # Robin Ashby voted on a matter naming Acme Development within the window
    store.add(Record("subj:robin-ashby", "vote", "https://canton.example/minutes/2026-10-20.pdf", title="Rezoning Z-26-07 for Acme Development, LLC",
                     text="Rezoning Z-26-07 for Acme Development, LLC", published="2026-10-20", target="rifle", evidence="primary",
                     meta={"vote": "yes", "result": "passed", "subject_id": "robin-ashby"}))
    store.flush()
    cands = [cand("robin-ashby", "Robin Ashby", office="U.S. House District 11"), cand("morgan-okafor", "Morgan Okafor", office="Mayor"),
             cand("taylor-calder", "Taylor Calder")]
    fin = finance.Finance(root, {}, fetcher, store, log=lambda *_: None)
    out = fin.run(cands, since="2025-01-01")
    assert out["imported"] == 2 and out["documents"] == {"reports": 1, "rows": 8} and out["fec"] == 1
    assert out["organizations"] >= 1
    contribs = store.query("kind='contribution'")
    assert len(contribs) == 11
    pat = store.read("subj:robin-ashby", "contribution")
    assert len(pat) == 9 and all(c["meta"]["subject_id"] == "robin-ashby" for c in pat)
    assert any(c["meta"]["needs_review"] for c in pat) and any(c["meta"]["filing_source"] == "FEC" for c in pat)
    reed = store.read("subj:morgan-okafor", "contribution")
    assert len(reed) == 2 and reed[0]["meta"]["recipient"] == "Morgan Okafor"       # "Morgan Okafor for Mayor" -> the candidate
    # the same John Smith across the city PDF, the state export and the FEC resolves to one entity
    ids = {c["meta"]["donor_entity"] for c in contribs if "smith" in c["meta"]["name"].lower()}
    assert len(ids) == 1
    # the PAC was looked up: what it is, who runs it, who it funds
    orgs = {o["title"]: o for o in store.query("kind='organization'")}
    pac = orgs["Mountain Realtors PAC"]
    assert pac["meta"]["fec_id"] == "C0002" and pac["meta"]["treasurer"] == "TAYLOR CALDER"
    assert any("Trade Association" in w["text"] for w in pac["meta"]["what"])
    assert pac["meta"]["recipients"][0]["name"] == "FRIENDS OF MORGAN OKAFOR"
    assert pac["meta"]["connected_org"] == "MOUNTAIN ASSOCIATION OF REALTORS"
    assert any("501(c)(6)" in w["text"] for w in pac["meta"]["what"])           # ProPublica matched the connected org by prefix? no: by name
    # the graph and its patterns
    ix = EntityIndex(root / "state" / "entities.json", store=store)
    g = connections.build(store, ix)
    pats = connections.patterns(g, store)
    kinds = {p["kind"] for p in pats}
    assert {"shared_donor", "employer_cluster", "officer_overlap", "donor_matter", "org_chain"} <= kinds
    shared = next(p for p in pats if p["kind"] == "shared_donor" and "smith" in p["donor_name"].lower())
    assert {r["name"] for r in shared["recipients"]} == {"Robin Ashby", "Morgan Okafor"} and shared["total"] == 1750.0
    cluster = next(p for p in pats if p["kind"] == "employer_cluster")
    assert cluster["recipient_name"] == "Robin Ashby" and len(cluster["donors"]) == 3 and "Acme" in cluster["employer"]
    officer = next(p for p in pats if p["kind"] == "officer_overlap")
    assert officer["person_name"] == "Taylor Calder" and officer["is_subject"]       # the PAC's treasurer is a candidate
    lead = next(p for p in pats if p["kind"] == "donor_matter")
    assert lead["evidence"] == "lead" and "Acme" in lead["term"] and lead["vote"] == "yes" and lead["days_apart"] == 40
    chain = next(p for p in pats if p["kind"] == "org_chain")
    assert [x["name"] for x in chain["path"]][0] == "Mountain Realtors PAC"
    money = connections.money_for(g, store, ix, "robin-ashby", pats)
    assert money["total_raised"] == 6050.0 and money["contributions"] == 8 and money["total_spent"] == 750.0
    assert money["organizations"][0]["name"] in ("Acme Development, LLC", "Mountain Realtors PAC")
    pacd = next(o for o in money["organizations"] if o["name"] == "Mountain Realtors PAC")
    assert pacd["officers"][0]["name"] == "Taylor Calder"
    assert {x["name"] for x in pacd["also_funds"]} == {"Morgan Okafor", "FRIENDS OF MORGAN OKAFOR"}   # state export + FEC schedule B
    counts = connections.export_graph(g, pats, tmp_path / "connections.json")
    assert counts["edges"] >= 15
    # second run: nothing re-parsed, nothing re-added
    out2 = fin.run(cands, since="2025-01-01")
    assert out2["imported"] == 0 and out2["documents"] == {"reports": 0, "rows": 0} and out2["fec"] == 0
    a = FEC_ROUTES["schedule_a"]
    assert a[-1]["min_date"] == ["2026-04-02"]                     # re-run starts at the cursor (minus 60 days), not 2025
    assert sum(1 for q in a if "last_index" not in q) == 2          # one first page per run; seek pagination after it
    store.close()
