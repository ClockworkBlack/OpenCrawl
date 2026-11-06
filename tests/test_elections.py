"""Locally run election results and qualifying lists -> contests -> archive shape. Synthetic pages, fictional people,
the Colorado example registry."""
from pathlib import Path

import pytest
import yaml

from opencrawl import derive
from opencrawl.civic import elections
from opencrawl.civic.seed import find_elections_site
from opencrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "examples" / "colorado-civic" / "config" / "jurisdictions.yaml"
JUR = {j["id"]: j for grp in ("counties", "municipalities") for j in yaml.safe_load(REGISTRY.read_text())[grp]}

TABLE_PAGE = """# November 4, 2025 Municipal General Election — Unofficial Results

Results as of 9:45 PM. 12 of 12 precincts reporting.

## Mayor - Town of Springfield

| Candidate | Votes | Percent |
| --- | --- | --- |
| Alex Rivera (I) | 612 | 58.3% |
| Pat Example | 437 | 41.7% |
| Total Votes Cast | 1,049 | |

## Council Member Ward 6 - Town of Springfield

| Candidate | Votes | Percent |
| --- | --- | --- |
| Chris T. Vale | 41 | 100% |

## Town of Vilas - Council Post 2

| Candidate | Votes | Percent |
| --- | --- | --- |
| Jane Q. Public | 120 | 44.1% |
| Robert "Bob" Roe | 98 | 36.0% |
| Sam Third | 54 | 19.9% |
| Write-in | 2 | |

## Precincts Reporting
| Precinct | Status |
| --- | --- |
| Springfield Town Hall | Complete |
"""

PDF_TEXT = """TOWN OF WALSH
OFFICIAL RESULTS - RUNOFF ELECTION
December 2, 2025

MAYOR
Mary Hill .......................... 88 52.4%
Joe Bass ........................... 80 47.6%
Total Votes Cast 168

COUNCIL POST 1
Ann Lee 101
Dan Moe 60
"""

QUALIFYING = """# Qualified Candidates — Nov. 4, 2025 Municipal General Election

Qualifying closed August 22, 2025.

Mayor
- Alex Rivera (incumbent)
- Pat Example

Council Member Ward 6
- Chris T. Vale (I)

Council Member Ward 4
| Name | Address |
| --- | --- |
| Lee Ward | 10 Main St |
| Kim Post | 22 Oak Ave |
"""


def test_detects_results_and_parses_tables():
    assert elections.looks_like_results("Unofficial Results", "https://x/results", TABLE_PAGE)
    assert not elections.looks_like_results("Parks", "https://x/parks", "Fun for all. " * 50)
    cs = elections.parse_results(TABLE_PAGE)
    assert [c["office"] for c in cs] == ["Mayor - Town of Springfield", "Council Member Ward 6 - Town of Springfield", "Town of Vilas - Council Post 2"]
    mayor = cs[0]
    assert mayor["total"] == 1049 and mayor["candidates"][0]["incumbent"] and mayor["candidates"][0]["outcome"] == "won"
    assert mayor["candidates"][1]["outcome"] == "" and mayor["candidates"][1]["pct"] == 41.7
    assert cs[1]["candidates"][0]["outcome"] == "unopposed"
    graham = cs[2]
    assert [x["name"] for x in graham["candidates"]] == ["Jane Q. Public", 'Robert "Bob" Roe', "Sam Third"]  # write-in dropped
    assert graham["candidates"][0]["outcome"] == "runoff" and graham["candidates"][1]["outcome"] == "runoff"
    assert elections.find_date(TABLE_PAGE, "") == "2025-11-04"


def test_parses_pdf_style_text_and_runoff():
    cs = elections.parse_results(PDF_TEXT)
    assert [c["office"] for c in cs] == ["MAYOR", "COUNCIL POST 1"]
    assert cs[0]["runoff"] is False  # heading itself isn't "runoff"; the election is, see derive
    assert cs[0]["candidates"][0]["name"] == "Mary Hill" and cs[0]["candidates"][0]["votes"] == 88 and cs[0]["total"] == 168
    assert cs[0]["candidates"][0]["outcome"] == "won"
    assert cs[1]["candidates"][1]["pct"] == 37.3 and cs[1]["candidates"][0]["outcome"] == "won"
    assert elections.find_date(PDF_TEXT) == "2025-12-02"


def test_qualifying_list():
    assert elections.looks_like_qualifying("Qualified Candidates", "https://x/q", QUALIFYING)
    q = elections.parse_qualifying(QUALIFYING)
    assert [c["office"] for c in q] == ["Mayor", "Council Member Ward 6", "Council Member Ward 4"]
    assert q[0]["candidates"][0] == {"name": "Alex Rivera", "party": "", "incumbent": True}
    assert q[2]["candidates"][1]["name"] == "Kim Post" and q[2]["candidates"][1]["note"] == "22 Oak Ave"


def test_derive_attributes_city_contests_and_exports_archive_shape(tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("co-baca:elections", "page", "https://baca.example/results", title="Unofficial Results",
                     text=TABLE_PAGE, target="co-baca", evidence="primary"))
    store.add(Record("walsh:site", "document", "https://walsh.example/runoff.pdf", title="Runoff results",
                     text=PDF_TEXT, target="walsh", evidence="primary"))
    store.add(Record("co-baca:elections", "page", "https://baca.example/qualified", title="Qualified Candidates",
                     text=QUALIFYING, target="co-baca"))
    store.flush()
    out = derive.run(store, only={"elections"}, context={"targets": list(JUR.values())}, log=lambda *_: None)
    assert out["elections"]["derived"] == 6  # 3 + 2 contests + 1 qualifying list
    res = store.query("kind='election_result'")
    by_office = {r["meta"]["office"]: r for r in res}
    assert by_office["Mayor - Town of Springfield"]["target"] == "springfield"      # county page, city contest
    assert by_office["Town of Vilas - Council Post 2"]["target"] == "vilas"
    assert by_office["MAYOR"]["target"] == "walsh" and by_office["MAYOR"]["meta"]["official"]
    assert derive.run(store, only={"elections"}, log=lambda *_: None)["elections"]["scanned"] == 0  # cursor advanced
    els = elections.export_elections(store, JUR)
    assert [e["eid"] for e in els] == ["xspringfield-2025-11-04", "xvilas-2025-11-04", "xwalsh-2025-12-02"]
    bax = els[0]
    assert bax["c"][0]["o"][0] == ["Alex Rivera", "", 612, 1, "won"] and bax["c"][0]["j"] == "Baca"
    assert bax["c"][0]["keys"] == ["ALEX RIVERA", "PAT EXAMPLE"] and bax["c"][0]["u"].endswith("/results")
    assert els[2]["kind"] == "municipal runoff" and els[2]["official"] is True and els[2]["c"][0]["o"][0][4] == "won"
    q = elections.export_qualifying(store)
    assert q[0]["target"] == "co-baca" and len(q[0]["contests"]) == 3
    store.close()


def test_find_elections_site():
    home = ("<html><body><a href='/departments/elections-registration'>Elections & Registration</a>"
            "<a href='https://www.coloradosos.gov/elections'>State elections</a><a href='/parks'>Parks</a></body></html>")
    assert find_elections_site(home, "https://www.bacacounty.example/") == "https://www.bacacounty.example/departments/elections-registration"
    home2 = "<html><body><a href='https://www.votecounty.gov/'>Board of Elections</a></body></html>"
    assert find_elections_site(home2, "https://www.county.example/") == "https://www.votecounty.gov/"
    assert find_elections_site("<a href='https://shop.example/vote-tshirts'>Vote</a>", "https://x.gov/") is None


NOTICE = """NOTICE OF SPECIAL ELECTION — TOWN OF SPRINGFIELD
A special election will be held on November 4, 2025.
Ballot Issue 2A: Shall the Town of Springfield be authorized to levy a 1 percent sales and use tax for a period of six years for the purpose of funding the following purposes: road improvements ($12,000,000), a new fire station, and park upgrades, not to exceed $30,000,000?
( ) YES ( ) NO
"""
QUESTION_RESULTS = """# November 4, 2025 Results — Unofficial
## Shall the Town of Springfield be authorized to levy a sales and use tax for six years?
| Choice | Votes | Percent |
| --- | --- | --- |
| Yes | 1,204 | 58.1% |
| No | 868 | 41.9% |
## Mayor
| Candidate | Votes | Percent |
| --- | --- | --- |
| Dana Reed | 1,500 | 100% |
"""


def test_ballot_questions_text_and_results(tmp_path):
    from opencrawl.civic import ballot

    qs = ballot.parse_questions(NOTICE)
    assert len(qs) == 1 and qs[0]["type"] == "Sales tax" and qs[0]["amount"] == "30,000,000" and qs[0]["years"] == "six" and qs[0]["place"] == "Springfield"
    store = Store(tmp_path / "data")
    store.add(Record("springfield:site", "document", "https://springfield.example/notice.pdf", title="Notice of Special Election", text=NOTICE,
                     target="springfield", evidence="primary"))
    store.add(Record("co-baca:elections", "page", "https://baca.example/results-2025", title="Unofficial Results", text=QUESTION_RESULTS,
                     target="co-baca", evidence="primary"))
    store.flush()
    out = derive.run(store, only={"elections", "ballot"}, context={"targets": list(JUR.values())}, log=lambda *_: None)
    assert out["elections"]["derived"] == 2 and out["ballot"]["derived"] == 1      # question result + mayor; question text
    qres = [r for r in store.query("kind='ballot_question'")]
    withres = next(r for r in qres if r["meta"]["has_results"])
    assert withres["target"] == "springfield" and withres["meta"]["yes"] == 1204 and withres["meta"]["result"] == "passed"
    assert withres["meta"]["type"] == "Sales tax" and withres["meta"]["date"] == "2025-11-04"
    assert len(store.query("kind='election_result'")) == 1                           # Yes/No contests are not candidate contests
    exp = ballot.export_questions(store, JUR)
    assert len(exp) == 1 and exp[0]["result"] == "passed" and exp[0]["amount"] == "30,000,000" and len(exp[0]["src"]) == 2
    assert exp[0]["question"].startswith("Shall the Town of Springfield be authorized to levy a 1 percent")
    els = elections.export_elections(store, JUR)
    q = next(c for e in els for c in e["c"] if c["sub"] == "question")
    assert q["key"] == "Baca|Sales tax" and q["o"] == [["Yes", "", 1204, 0, ""], ["No", "", 868, 0, ""]] and q["res"] == "passed"
    store.close()
