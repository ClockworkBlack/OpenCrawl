"""Minutes -> motions and votes; Legistar sponsors; stances / ideology / endorsements / ratings / career;
the profile supplement; export of profiles and connections. Fictional names; local stubs."""
import http.server
import json
import threading
from pathlib import Path

import pytest
import requests

from opencrawl import derive
from opencrawl.civic import minutes
from opencrawl.research import stances
from opencrawl.research.subjects import Researcher
from opencrawl.fetch import Fetcher
from opencrawl.names import name_variants
from opencrawl.adapters.legistar import fill_sponsors
from opencrawl.store import Record, Store

MINUTES = """CITY OF CANTON
REGULAR COUNCIL MEETING MINUTES
October 20, 2026

Members present: Mayor Dana Reed, Council Member Pat Local, Council Member Kim Post, Council Member Lee Ward, Council Member Sam Third. Absent: none.

4. Ordinance No. 2026-14, amending Chapter 18 regarding short-term rentals.
Motion by Council Member Local, seconded by Council Member Post, to adopt Ordinance No. 2026-14. Motion carried 3-1 (Ward opposed).

5. Resolution 2026-40, FY27 budget amendment.
Council Member Ward moved to approve Resolution 2026-40, seconded by Council Member Third. Ayes: Ward, Third, Post. Nays: Local. Motion passed.

6. Rezoning application Z-26-07 for 12 acres on Elm Street for a data center.
A motion to table the rezoning was made by Council Member Third, seconded by Council Member Local. The motion carried unanimously.

7. Discussion of the parks master plan. No action was taken.

8. Alcohol license for Elm Street Tavern. Motion: Post. Second: Third. Roll call: Local - Yes; Post - Yes; Third - Yes; Ward - No. Motion carried 3-1.

9. Motion by Council Member Ward to adjourn. Seconded by Council Member Local. Vote: 4-0.
"""

SITE_ABOUT = """About Pat Local. I am a lifelong conservative Republican and a member of the Garfield County Republican Party.
I served on the Rifle Planning Commission from 2014 to 2018 and was elected to the City Council in 2019.
I will fight to lower property taxes and keep our millage rate flat. I oppose the proposed data center on Elm Street until a water study is done.
I believe every child deserves a great public school, and I support raising teacher pay.
Proud to be endorsed by the Mountain Chamber of Commerce and Sheriff Dana Reed. I earned an A rating from the NRA in 2024.
Click here to donate now. Paid for by Friends of Pat Local."""

ARTICLE = """CANTON — Council member Pat Local said Tuesday she would vote against the rezoning for the Acme data center.
"We cannot support this without a water study," Local told the Tribune. The Mountain Chamber endorsed Local in September.
Her opponent, Kim Post, supports the project. Local describes herself as a fiscal conservative."""

TRANSCRIPT = """[00:00:03] MAYOR: Good evening.
[00:31:40] PAT LOCAL: I cannot support a data center on that parcel without a water study.
[00:32:10] KIM POST: I support it, we need the tax base.
[01:10:02] CLERK: Motion passes."""

LEGISTAR = {
    "/v1/canton/matters/5/sponsors": [{"MatterSponsorName": "Pat Local", "MatterSponsorNameId": 11, "MatterSponsorSequence": 1},
                                      {"MatterSponsorName": "Kim Post", "MatterSponsorNameId": 12, "MatterSponsorSequence": 2}],
    "/v1/canton/matters/6/sponsors": [],
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
        elif path in LEGISTAR:
            body, ct = json.dumps(LEGISTAR[path]).encode(), "application/json"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)

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


def test_minutes_motions_and_votes():
    ms = minutes.parse_motions(MINUTES)
    assert [m["ordinance_ref"] for m in ms[:2]] == ["Ordinance 2026-14", "Resolution 2026-40"]
    m4 = ms[0]
    assert m4["mover"] == "Local" and m4["seconder"] == "Post" and m4["result"] == "passed" and m4["tally"] == "3-1"
    v = {x["person"]: (x["vote"], x["basis"]) for x in m4["votes"]}
    assert v == {"Lee Ward": ("no", "named"), "Pat Local": ("yes", "moved"), "Kim Post": ("yes", "seconded"), "Sam Third": ("yes", "tally")}
    assert "Dana Reed" not in v                                   # the mayor presides; 3-1 adds up without her
    m5 = ms[1]
    assert {x["person"]: x["vote"] for x in m5["votes"]} == {"Lee Ward": "yes", "Sam Third": "yes", "Kim Post": "yes", "Pat Local": "no"}
    m6 = ms[2]
    assert m6["unanimous"] and m6["motion"].startswith("table the rezoning")
    assert {x["person"] for x in m6["votes"]} == {"Pat Local", "Kim Post", "Lee Ward", "Sam Third"}
    assert next(x for x in m6["votes"] if x["person"] == "Kim Post")["basis"] == "unanimous"
    m8 = ms[3]
    assert m8["motion"] == "Alcohol license for Elm Street Tavern" and {x["person"]: x["vote"] for x in m8["votes"]}["Lee Ward"] == "no"
    assert ms[4]["procedural"] and ms[4]["result"] == "passed"
    assert all(not m["procedural"] for m in ms[:4])


def test_profile_supplement_end_to_end(server, fetcher, tmp_path):
    root = tmp_path / "proj"
    base = Path(__file__).resolve().parent.parent
    (root / "config").mkdir(parents=True)
    for f in ("outlets.yaml", "topics.yaml", "issues.yaml", "finance.yaml", "organizations.yaml"):
        (root / "config" / f).write_text((base / "config" / f).read_text())
    (root / "config" / "jurisdictions.yaml").write_text((base / "examples" / "colorado-civic" / "config" / "jurisdictions.yaml").read_text())
    store = Store(root / "data")
    # official records: minutes PDF, a Legistar ordinance (sponsors to fill), a transcript
    store.add(Record("canton:site", "document", "https://canton.example/minutes/2026-10-20.pdf", title="October 20, 2026 Council Minutes",
                     text=MINUTES, published="2026-10-20", target="rifle", channel="pdf", evidence="primary", meta={"doc_type": "minutes"}))
    base = server + "/v1/canton"
    for mid, title, mtype in ((5, "An ordinance to require a water study for data centers", "Ordinance"), (6, "Approving the FY27 budget", "Resolution")):
        store.add(Record("canton:legistar", "ordinance" if mtype == "Ordinance" else "matter", f"{base}/matters/{mid}", title=title, text=title,
                         published="2026-09-01", target="rifle", channel="api", evidence="primary",
                         meta={"MatterId": mid, "MatterFile": f"26-{mid}", "MatterTypeName": mtype, "MatterStatusName": "Adopted",
                               "MatterEnactmentDate": "2026-09-15", "MatterLastModifiedUtc": "2026-09-16T00:00:00", "provider": "legistar", "opened": True}))
    store.add(Record("canton:granicus", "transcript", "https://canton.example/TranscriptViewer.php?clip_id=77", title="Transcript",
                     text=TRANSCRIPT, published="2026-10-20", target="rifle", evidence="primary",
                     meta={"body": "City Council", "meeting_date": "2026-10-20", "video_page": "https://canton.example/MediaPlayer.php?clip_id=77"}))
    store.flush()
    # derive minutes -> votes; fill sponsors from the (stub) Legistar API
    out = derive.run(store, only={"minutes"}, log=lambda *_: None)
    assert out["minutes"]["derived"] == 10                         # 5 motions -> 5 agenda items + 5 votes
    sp = fill_sponsors(fetcher, store, log=lambda *_: None)
    assert sp == {"checked": 2, "with_sponsors": 1}
    ords = {r["meta"]["MatterId"]: r for r in store.query("kind IN ('ordinance','matter')")}
    assert ords[5]["meta"]["sponsors"][0]["name"] == "Pat Local" and ords[5]["meta"]["opened_url"].endswith("/matters/5/sponsors")
    assert fill_sponsors(fetcher, store, log=lambda *_: None)["checked"] == 0          # not re-fetched
    # the candidate: own site statement, a reported article, then research (no network steps)
    cand = {"id": "pat-local", "name": "Pat Local", "aliases": [], "websites": [], "social": [], "targets": ["rifle"],
            "target": "rifle", "office": "City Council Post 2", "party": "R",
            "races": [{"eid": "2023Nov", "d": "2023-11-07", "office": "City Council Post 2 - Rifle", "county": "Garfield", "party": "",
                       "votes": 812, "pct": 61.2, "outcome": "won", "level": "local", "u": "https://results.sos.example/x/1"},
                      {"eid": "2019Nov", "d": "2019-11-05", "office": "City Council Post 2 - Rifle", "county": "Garfield", "party": "",
                       "votes": 500, "pct": 55.0, "outcome": "won", "level": "local", "u": "https://results.sos.example/x/2"}],
            "variants": name_variants("Pat Local")}
    store.add(Record("subj:pat-local", "statement", "https://patlocal.example/about", title="About Pat", text=SITE_ABOUT,
                     published="2026-08-01", target="rifle", channel="html", evidence="primary", meta={"subject_id": "pat-local", "opened": True}))
    store.add(Record("subj:pat-local", "article", "https://tribune.example/2026/10/local-data-center", title="Local opposes data center",
                     text=ARTICLE, published="2026-10-21", target="rifle", channel="html", evidence="reported",
                     meta={"subject_id": "pat-local", "outlet": "tribune.example", "opened": True}))
    store.flush()
    r = Researcher(root, {}, fetcher, store, log=lambda *_: None)
    counts = r.research(cand, skip={"posts", "statements", "articles", "leads", "social_mentions", "connections"})
    assert counts["vote"] == 5 and counts["sponsorship"] == 1 and counts["mention"] >= 1
    votes = {v["title"][:21]: v["meta"] for v in store.read("subj:pat-local", "vote")}
    assert votes["adopt Ordinance No. 2"]["vote"] == "yes" and votes["adopt Ordinance No. 2"]["basis"] == "moved"
    assert votes["approve Resolution 20"]["vote"] == "no" and votes["approve Resolution 20"]["basis"] == "named"
    # the profile supplement
    prof = stances.profile_supplement(cand, store, root)
    topics = {p["topic"] for p in prof["positions"]}
    assert "Taxes and budget" in topics and "Education" in topics and ("Energy and utilities" in topics or "Growth and development" in topics)
    own = [p for p in prof["positions"] if p["basis"] == "campaign site"]
    said = [p for p in prof["positions"] if p["basis"].startswith("said at City Council")]
    rep = [p for p in prof["positions"] if p["basis"].startswith("reported by")]
    assert own and said and rep and said[0]["needs_review"] and said[0]["d"] == "2026-10-20"
    assert all(p["evidence"] == "reported" for p in rep) and any("water study" in p["stance"] for p in rep)
    assert "lifelong conservative republican" in prof["ideology"] and prof["ideology"].startswith("Republican; self-described")
    assert "NRA A" in prof["ideology"] and "Mountain Chamber" in prof["ideology"] and prof["isrc"]
    assert {e for e in prof["endorsements"]} >= {"Mountain Chamber of Commerce (2026)", "Sheriff Dana Reed (2026)", "Mountain Chamber (2026)"}
    assert prof["ratings"][0] == {"org": "NRA", "rating": "A", "year": "2024", "src": [0], "basis": "campaign site (self-reported)"}
    assert {a["org"] for a in prof["affiliations"]} >= {"Garfield County Republican Party", "Rifle Planning Commission"}
    assert [v["vote"] for v in prof["votes"]].count("yes") >= 3 and prof["bills"][0]["number"] == "26-5" and prof["bills"][0]["role"] == "sponsor"
    assert {o["bill"] for o in prof["ordinances_supported"]} == {"Ordinance 2026-14", "26-5"}
    assert {o["bill"] for o in prof["ordinances_opposed"]} == {"Resolution 2026-40"}
    assert prof["inference_inputs"] >= 10 and prof["depth"] == "full"
    # sources: every src index points at an opened record; none unverified
    for p in prof["positions"] + prof["statements"]:
        for i in p["src"]:
            assert prof["sources"][i]["u"].startswith("http") and prof["sources"][i]["evidence"] in ("primary", "reported")
    # career timeline
    from opencrawl.research.careers import career_for, summary

    S = stances.SourceList()
    S.items = prof["sources"]
    S.index = {k: i for i, k in enumerate(k for k in [s["u"].replace("https://", "").rstrip("/").lower() for s in S.items])}
    career = career_for(cand, store, S, prof["affiliations"])
    kinds = [(e["kind"], e["d"][:4]) for e in career]
    assert ("election", "2019") in kinds and ("election", "2023") in kinds
    assert any(e["kind"] == "bio" and e["body"] == "Rifle Planning Commission" and e["d"] == "2014" and e["to"] == "2018" for e in career)
    assert any(e["kind"] == "bio" and e["body"] == "City Council" and e["how"].startswith("elected") and e["d"] == "2019" for e in career)
    att = next(e for e in career if e["kind"] == "attendance")
    assert att["d"] == "2026-10-20" and att["target"] == "rifle"
    assert summary(career).startswith("won City Council Post 2 - Rifle (2019); City Council Post 2 - Rifle (2023)")
    # export: profiles/, connections.json, packets carry the profile
    from opencrawl.export import export

    (root / "config" / "subjects.yaml").write_text(json.dumps({"candidates": [{k: v for k, v in cand.items() if k != "variants"}]}))
    man = export(root, tmp_path / "dist", log=lambda *_: None, store=store, cfg={})
    assert man["profile_supplements"] == 1 and man["connections"]["edges"] >= 1
    pk = json.loads((tmp_path / "dist" / "packets" / "pat-local.json").read_text())
    assert pk["profile"]["depth"] == "full" and pk["counts"]["inference_inputs"] >= 10
    idx = json.loads((tmp_path / "dist" / "profiles" / "index.json").read_text())
    assert idx["profiles"][0]["subject_id"] == "pat-local" and idx["thin"] == 0
    # unchanged data -> byte-identical files (so a downstream repository only sees real changes)
    export(root, tmp_path / "dist2", log=lambda *_: None, store=store, cfg={})
    for rel in ("packets/pat-local.json", "profiles/pat-local.json", "profiles/index.json", "connections.json"):
        assert (tmp_path / "dist" / rel).read_bytes() == (tmp_path / "dist2" / rel).read_bytes(), rel
    store.close()
