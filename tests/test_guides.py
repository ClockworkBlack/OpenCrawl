"""Endorsements, scorecards and questionnaires -> per-candidate support records -> profile supplement. Fictional."""
import json
from pathlib import Path

from opencrawl import derive
from opencrawl.civic import guides, registry
from opencrawl.research import stances
from opencrawl.names import name_variants
from opencrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "colorado-civic"


def _copy_config(root, names):
    (root / "config").mkdir(parents=True, exist_ok=True)
    for f in names:
        src = EXAMPLE / "config" / f if f in ("jurisdictions.yaml", "press.yaml", "guides.yaml") else ROOT / "config" / f
        (root / "config" / f).write_text(src.read_text())
ENDORSE_PAGE = """2026 General Election Endorsements
The Mountain Chamber is proud to endorse Pat Local for City Council Post 2.
- Dana Reed – Mayor of Rifle
- Kim Post (State House District 23)
Click here to donate.
"""
SCORECARD = """2025 Legislative Scorecard
| Legislator | Party | District | Score |
| --- | --- | --- | --- |
| Kim Post | R | House 23 | 92% |
| Pat Local | D | Senate 7 | 45% |
"""
QUESTIONNAIRE = """Rifle City Council Post 2 — Candidate Questionnaire
Question 1: What is your top priority for the city?
Pat Local: Fixing our roads and keeping taxes flat. I will fight for a six-year paving plan funded by the existing SPLOST.
Dana Reed: Public safety. We need more officers on patrol and a new fire station on Elm Street.

Question 2: Do you support the proposed data center?
Pat Local: No, not without a water study. I oppose rezoning that parcel until the county finishes the study.
"""


def test_parsers():
    e = {x["name"]: x["office"] for x in guides.parse_endorsements(ENDORSE_PAGE)}
    assert e == {"Pat Local": "City Council Post 2", "Dana Reed": "Mayor of Rifle", "Kim Post": "State House District 23"}
    r = {x["name"]: x["rating"] for x in guides.parse_ratings(SCORECARD)}
    assert r == {"Kim Post": "92%", "Pat Local": "45%"}
    q = guides.parse_questionnaire(QUESTIONNAIRE)
    assert [x["question"] for x in q] == ["What is your top priority for the city?", "Do you support the proposed data center?"]
    assert q[1]["answers"][0]["name"] == "Pat Local" and q[1]["answers"][0]["answer"].startswith("No, not without")
    assert any(s["id"] == "guide:lwv-co" and s["kind"] == "guide" for s in registry.guide_sources(EXAMPLE))


def test_guides_derive_into_profiles(tmp_path):
    root = tmp_path / "proj"
    _copy_config(root, ("jurisdictions.yaml", "outlets.yaml", "topics.yaml", "issues.yaml"))
    cand = {"id": "pat-local", "name": "Pat Local", "target": "rifle", "targets": ["rifle"], "office": "City Council Post 2",
            "party": "D"}
    (root / "config" / "subjects.yaml").write_text(json.dumps({"subjects": [cand]}))
    store = Store(root / "data")
    for sid, name, kind, url, text in (("guide:chamber", "Mountain Chamber of Commerce", "endorsements", "https://chamber.example/endorsements", ENDORSE_PAGE),
                                       ("guide:cva", "Conservation Voters Alliance", "scorecard", "https://gcv.example/scorecard-2025", SCORECARD),
                                       ("guide:cvl", "Civic Voters League", "questionnaire", "https://lwv.example/rifle-post-2", QUESTIONNAIRE)):
        store.add(Record(sid, "guide", url, title=text.splitlines()[0], text=text, published="2026-09-15", channel="html", evidence="primary",
                         meta={"guide_name": name, "guide_kind": kind, "opened": True}))
    store.flush()
    out = derive.run(store, only={"guides"}, context={"root": root, "cfg": {"electo": {"levels": []}}}, log=lambda *_: None)
    assert out["guides"]["derived"] == 3 + 1 + 2 + 1 + 2       # 3 endorsements (+1 candidate link), 2 ratings (+1 link), 2 answers
    conns = {(r["meta"]["type"], r["meta"]["party"]): r for r in store.read("subj:pat-local", "connection")}
    assert conns[("endorsement", "Mountain Chamber of Commerce")]["meta"]["basis"] == "endorser's own page"
    assert conns[("rating", "Conservation Voters Alliance")]["meta"]["rating"] == "45%"
    answers = store.read("subj:pat-local", "guide_answer")
    assert len(answers) == 2 and all(a["meta"]["guide"] == "Civic Voters League" for a in answers)
    unmatched = [r for r in store.query("kind='endorsement'") if not r["meta"]["matched"]]
    assert {r["meta"]["name"] for r in unmatched} == {"Dana Reed", "Kim Post"}       # listed for review, not invented
    c = {**cand, "aliases": [], "websites": [], "social": [], "variants": name_variants("Pat Local")}
    prof = stances.profile_supplement(c, store, root)
    assert prof["endorsements"] == ["Mountain Chamber of Commerce (2026)"] and prof["ratings"][0]["rating"] == "45%"
    qa = [p for p in prof["positions"] if p["basis"].startswith("questionnaire")]
    assert qa and any("water study" in p["stance"] for p in qa) and any(p["topic"] in ("Growth and development", "Energy and utilities", "Water") for p in qa)
    assert "Conservation Voters Alliance 45%" in prof["ideology"] and "endorsed by Mountain Chamber of Commerce" in prof["ideology"]
    assert prof["inference_inputs"] >= 5
    store.close()
