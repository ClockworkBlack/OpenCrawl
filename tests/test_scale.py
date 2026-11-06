"""Registry, discovery parsing, block handling, scheduler, subject grading, quarantine, export — all offline."""
import datetime as dt
import http.server
import json
import threading
from pathlib import Path

import pytest
import requests
import yaml

from opencrawl import seed
from opencrawl.civic import registry
from opencrawl.blocked import BlockList, Blocked, looks_blocked, manual_tasks_md
from opencrawl.research.quarantine import Quarantine
from opencrawl.research.subjects import Classifier, Researcher, domain_of, load_subjects
from opencrawl.fetch import Fetcher
from opencrawl.scheduler import Progress, due, run_sources
from opencrawl.store import Record, Store
from opencrawl.verified_posts import Registry

ROOT = Path(__file__).resolve().parent.parent

CITY_HOME = b"""<html><head><title>City of Testville</title>
<link rel="alternate" type="application/rss+xml" href="/news.rss"></head><body>
<a href="https://testville.legistar.com/Calendar.aspx">Agendas &amp; Minutes</a>
<a href="https://library.municode.com/co/testville">Code of Ordinances</a>
<a href="https://www.facebook.com/CityofTestville">Facebook</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
<a href="/AgendaCenter">Agenda Center</a>
<a href="/government/city-council">City Council</a>
<a href="/parks">Parks</a></body></html>"""

SITE = {
    "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
    "/": (200, "text/html", CITY_HOME),
    "/AgendaCenter": (200, "text/html", b"<html><body>AgendaCenter <a href='/RSSFeed.aspx?ModID=65'>RSS</a></body></html>"),
    "/government/city-council": (200, "text/html", b"<html><body><h1>City Council</h1><p>Meetings are on Granicus: "
                                                    b"<a href='https://testville.granicus.com/ViewPublisher.php?view_id=2'>Agendas and video</a></p>"
                                                    b"<a href='https://go.boarddocs.com/co/testville/Board.nsf/Public'>Board portal</a></body></html>"),
    "/blockedsite/": (403, "text/html", b"<html><body><h2>Access blocked</h2><script>__cf_chl</script></body></html>"),
    "/campaign": (200, "text/html", b"<html><body><main><p>Jane Doe for Council. Endorsed by Testville Firefighters Local 99. "
                                     b"Follow on <a href='https://bsky.app/profile/jane.bsky.social'>Bluesky</a>.</p></main></body></html>"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/blockedsite"):
            path = "/blockedsite/"
        if path not in SITE:
            self.send_response(404); self.end_headers(); return
        status, ctype, body = SITE[path]
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def server():
    srv = _serve()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def server2():
    """A second host, so blocking it doesn't block `server`."""
    srv = _serve()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("TestCrawler/0.1", tmp_path / "state", min_interval=0, session=s, sleep=lambda s: None)


# ----------------------------------------------------------------------------- registry
EXAMPLE = ROOT / "opencrawl" / "examples" / "colorado-civic"


def test_registry_is_complete():
    jur = registry.jurisdictions(EXAMPLE)
    kinds = {}
    for j in jur:
        kinds[j["kind"]] = kinds.get(j["kind"], 0) + 1
    assert kinds["county"] == 64
    assert kinds["city"] + kinds["town"] + kinds["consolidated"] == 272
    assert kinds["school_district"] == 178
    assert all(j["fips"] for j in jur if j["kind"] == "county")
    assert len({j["id"] for j in jur}) == len(jur)
    sd = next(j for j in jur if j["id"] == "sd-jefferson-county-school-district-r-1")
    assert sd["county"] == "Jefferson" and sd["county_id"] == "co-jefferson" and sd["tier"] == "b"
    ids = {s["id"] for s in registry.sources_for({**sd, "website": "https://www.jeffco-schools.example/",
                                                  "portals": {"boarddocs": "https://go.boarddocs.com/co/jeffco/Board.nsf/Public"}})}
    assert ids == {"sd-jefferson-county-school-district-r-1:boarddocs", "sd-jefferson-county-school-district-r-1:site"}
    den = next(j for j in jur if j["id"] == "denver")
    assert den["tier"] == "a" and den["kind"] == "consolidated" and "Denver" in den["counties"]
    assert next(j for j in jur if j["id"] == "co-denver")["fips"] == "08031"
    small = next(j for j in jur if j["id"] == "bonanza")
    assert small["tier"] == "c" and small["wikipedia"].endswith("/Bonanza,_Colorado")


def test_generic_targets_and_overrides(tmp_path):
    from opencrawl import targets as T

    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "targets.yaml").write_text(yaml.safe_dump({"targets": [
        {"id": "acme", "name": "Acme Corporation", "kind": "company", "website": "https://www.acme.example/", "tier": "a",
         "include": ["news", "press"], "portals": {"youtube": "https://www.youtube.com/@acme"}},
        {"id": "nowhere", "name": "Nowhere Fund", "kind": "organization"}]}))
    (root / "state").mkdir()
    (root / "state" / "discovered.json").write_text(json.dumps({"nowhere": {"website": "https://nowhere.example/", "portals": {}, "feeds": [],
                                                                            "checked_at": "2026-01-01T00:00:00+00:00"}}))
    (root / "config" / "overrides.yaml").write_text(yaml.safe_dump({"targets": {"nowhere": {"tier": "c", "notes": "hand-checked"}}}))
    (root / "config" / "sources.yaml").write_text(yaml.safe_dump({"sources": [
        {"id": "standalone", "url": "https://feed.example/news", "kind": "article"},
        {"id": "acme:site", "max_pages": 5}]}))
    tg = {t["id"]: t for t in T.targets(root)}
    assert tg["acme"]["tier"] == "a" and tg["nowhere"]["website"] == "https://nowhere.example/" and tg["nowhere"]["tier"] == "c"
    srcs = {s["id"]: s for s in T.all_sources(root)}
    assert set(srcs) == {"acme:youtube", "acme:site", "nowhere:site", "standalone"}
    assert srcs["acme:site"]["max_pages"] == 5 and srcs["acme:site"]["include"] == ["news", "press"]
    assert srcs["acme:youtube"]["endpoint"] == "youtube:https://www.youtube.com/@acme" and "linked from" in srcs["acme:youtube"]["youtube_basis"]
    assert srcs["nowhere:site"]["exclude"] and srcs["standalone"]["tier"] == "b" and srcs["standalone"]["target"] == ""
    assert T.sources_for({**tg["acme"], "enabled": False}) == []


def test_sources_expand_from_portals():
    j = {"id": "testville", "name": "Testville", "kind": "city", "population": 12000, "tier": "a",
         "website": "https://www.testville.gov/", "portals": {"legistar": "https://testville.legistar.com/x",
                                                               "agenda_center": "https://www.testville.gov/AgendaCenter",
                                                               "municode": "https://library.municode.com/co/testville"}}
    ids = {s["id"]: s for s in registry.sources_for(j)}
    assert set(ids) == {"testville:legistar", "testville:agendas", "testville:site"}  # municode is never crawled
    assert ids["testville:legistar"]["legistar"] == "testville"
    assert registry.sources_for({**j, "enabled": False}) == []


def test_find_portals():
    p = registry.find_portals(CITY_HOME.decode(), "https://www.testville.gov/")
    assert p["legistar"].startswith("https://testville.legistar.com")
    assert p["municode"] == "https://library.municode.com/co/testville"
    assert p["facebook"] == "https://www.facebook.com/CityofTestville"
    assert "agenda_center" not in p  # relative link; found by the AgendaCenter probe instead


# ----------------------------------------------------------------------------- seed
def test_inspect_site_finds_agenda_center_and_feed(server, fetcher):
    out = seed.inspect_site(fetcher, server + "/")
    assert out["portals"]["agenda_center"] == server + "/AgendaCenter"
    assert out["portals"]["legistar"].startswith("https://testville.legistar.com")
    assert any(f.endswith("/news.rss") for f in out["feeds"]) and any("RSSFeed.aspx" in f for f in out["feeds"])
    # portals linked only from the council page are found too (Granicus, BoardDocs)
    assert out["portals"]["granicus"].startswith("https://testville.granicus.com") and "boarddocs" in out["portals"]
    assert out["section_page"] == server + "/government/city-council"
    pages = seed.follow_pages(CITY_HOME.decode(), server + "/")
    assert pages == [server + "/AgendaCenter", server + "/government/city-council"]          # agendas first; parks never


# ----------------------------------------------------------------------------- blocked
def test_block_detection_and_list(server2, fetcher, tmp_path):
    server = server2
    assert looks_blocked(403, b"<h2>Access blocked</h2> __cf_chl") and not looks_blocked(200, b"Access blocked")
    assert looks_blocked(503, b"Just a moment... challenge-platform")
    with pytest.raises(Blocked):
        fetcher.get(server + "/blockedsite/page1")
    assert fetcher.blocklist.is_blocked(server + "/anything")  # whole host now skipped
    with pytest.raises(Blocked):
        fetcher.get(server + "/")  # no request made; the host is blocked
    md = manual_tasks_md(fetcher.blocklist, [{"id": "x", "name": "X", "kind": "city", "portals": {},
                                              "municode": "https://library.municode.com/co/x", "population": 5}],
                         law={"law": "the Colorado Open Records Act", "citation": "C.R.S. § 24-72-201 et seq.", "response_days": 3})
    assert "Blocked hosts" in md and "Colorado Open Records Act" in md and "three business days" in md.replace("3 business days", "three business days")
    assert "library.municode.com/co/x" in md and "C.R.S." in md
    fetcher.blocklist.clear(BlockList.host(server))
    assert fetcher.get(server + "/").status == 200


# ----------------------------------------------------------------------------- scheduler
def test_due_ordering_and_cadence(tmp_path):
    prog = Progress(tmp_path / "p.json")
    now = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
    srcs = [{"id": "big:site", "tier": "a"}, {"id": "small:site", "tier": "c"}, {"id": "mid:site", "tier": "b"}]
    prog.update("big:site", last_ok=(now - dt.timedelta(hours=12)).isoformat())   # not due (daily)
    prog.update("small:site", last_ok=(now - dt.timedelta(days=40)).isoformat())  # overdue (monthly)
    prog.update("mid:site", last_ok=(now - dt.timedelta(days=2)).isoformat())     # not due (weekly)
    assert [s["id"] for s in due(srcs, prog, now=now)] == ["small:site"]
    assert len(due(srcs, prog, now=now, force=True)) == 3
    prog.update("mid:site", last_ok=None, last_run=(now - dt.timedelta(hours=1)).isoformat(), status="error (html): boom")
    assert "mid:site" not in {s["id"] for s in due(srcs, prog, now=now)}  # failing source backs off
    prog.update("mid:site", last_run=(now - dt.timedelta(days=3)).isoformat())
    assert [s["id"] for s in due(srcs, prog, now=now)][0] == "mid:site"  # never succeeded = most overdue


def test_run_sources_records_progress(server, server2, fetcher, tmp_path):
    from opencrawl import channels

    store = Store(tmp_path / "data")
    prog = Progress(tmp_path / "progress.json")
    cache = channels.ChannelCache(tmp_path / "channels.json")
    srcs = [{"id": "testville:site", "url": server + "/", "kind": "page", "tier": "a", "target": "testville", "depth": 1},
            {"id": "blocked:site", "url": server2 + "/blockedsite/", "kind": "page", "tier": "a", "target": "blocked"}]
    cfg = {"schedule": {"windows": []}, "limits": {}}
    summary = run_sources(cfg, srcs, fetcher, store, cache, prog, deadline=1e12, workers=2, log=lambda *_: None)
    assert prog.get("testville:site")["status"] == "ok"
    assert summary["blocked:site"].startswith("blocked")
    recs = store.read("testville:site", "page")
    assert recs and recs[0]["target"] == "testville" and recs[0]["evidence"] == "primary"


# ----------------------------------------------------------------------------- subjects + quarantine
def test_classifier_and_quarantine(tmp_path):
    reg = Registry(tmp_path / "verified")
    acc = reg.add_account("Jane Doe", "https://bsky.app/profile/jane.bsky.social", "https://janedoe.org")
    reg.mark_subject(acc["id"], "jane-doe")
    reg.mark_account(acc["id"], "verified", "checked")
    cand = {"id": "jane-doe", "name": "Jane Doe", "websites": ["https://janedoe.org"]}
    cls = Classifier(ROOT, cand, {"website": "https://www.testville.gov/"}, reg)
    assert cls.classify("https://www.testville.gov/council/minutes.pdf") == "primary"
    assert cls.classify("https://janedoe.org/issues") == "primary"
    assert cls.classify("https://bsky.app/profile/jane.bsky.social/post/1") == "primary"
    assert cls.classify("https://bsky.app/profile/random.bsky.social/post/1") == "unverified"
    assert cls.classify("https://apnews.com/politics/story") == "reported"
    assert cls.classify("https://someblog.example/jane-doe-scandal") == "unverified"
    assert cls.excluded("https://ballotpedia.org/Jane_Doe") and not cls.excluded("https://apnews.com/x")
    assert domain_of("https://amp.news.example.co.uk/a") == "example.co.uk"

    rm = Quarantine(tmp_path)
    r = rm.add("jane-doe", "https://someblog.example/post", "Doe took a bribe", origin="article", author="anon")
    assert r["status"] == "unverified" and rm.add("jane-doe", "https://someblog.example/post", "", "article")["id"] == r["id"]
    rm.set_status(r["id"], "debunked", evidence_url="https://apnews.com/fact-check", note="no such payment")
    assert rm.all()[0]["status"] == "debunked" and rm.all()[0]["corroboration"][0]["url"].endswith("fact-check")


def test_research_routes_unverified_to_quarantine(server, fetcher, tmp_path, monkeypatch):
    # a tiny project root: real outlets.yaml, a registry with one target, one candidate
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "outlets.yaml").write_text((ROOT / "config" / "outlets.yaml").read_text())
    (root / "config" / "jurisdictions.yaml").write_text(yaml.safe_dump({"counties": [], "municipalities": [
        {"id": "testville", "name": "Testville", "kind": "city", "fips": "0800001", "counties": ["Baca"], "population": 100}]}))
    (root / "config" / "subjects.yaml").write_text(yaml.safe_dump({"subjects": [
        {"id": "jane-doe", "name": "Jane Doe", "office": "Council Member", "target": "testville", "websites": [server + "/campaign"],
         "social": ["https://bsky.app/profile/jane.bsky.social"]}]}))
    store = Store(root / "data")
    # an official record that mentions her, as the target crawl would have stored it
    store.add(Record("testville:agendas", "agenda_item", "https://www.testville.gov/a/1", title="Motion by Jane Doe to adopt budget",
                     text="Councilmember Jane Doe moved to adopt the FY27 budget. Passed 5-0.", target="testville"))
    r = Researcher(root, {}, fetcher, store, log=lambda *_: None)
    cand = load_subjects(root)[0]
    counts = r.research(cand, skip={"articles", "leads", "votes", "social_mentions", "connections"})
    assert counts.get("statement", 0) >= 1 and counts.get("mention") == 1 and counts.get("connection") == 1
    conn = store.read("subj:jane-doe", "connection")[0]
    assert conn["meta"]["type"] == "endorsement" and "Firefighters" in conn["meta"]["party"]
    # unverified article -> quarantine, reported article -> store
    r.quarantine = Quarantine(root)
    cls = Classifier(root, cand, None, r.registry)
    counts2 = {}
    rec_bad = Record("", "article", "https://randomblog.example/doe", title="Doe did a bad thing")
    rec_good = Record("", "article", "https://apnews.com/politics/doe", title="Doe wins")
    for rec in (rec_bad, rec_good):
        rec.source_id = "subj:jane-doe"; rec.meta["subject_id"] = "jane-doe"; rec.evidence = cls.classify(rec.url)
        if rec.evidence == "unverified":
            r.quarantine.add("jane-doe", rec.url, rec.title, "article")
        else:
            store.add(rec)
    r.quarantine.flush()
    assert [x["url"] for x in r.quarantine.all()] == ["https://randomblog.example/doe"]
    assert [x["url"] for x in store.read("subj:jane-doe", "article")] == ["https://apnews.com/politics/doe"]

    from opencrawl.export import export

    man = export(root, root / "dist", log=lambda *_: None, store=store)
    assert man["targets"] == 1 and man["subjects_with_records"] == 1
    assert man["quarantine"] == {"claims": {"unverified": 1}, "videos": 0, "exported": False}
    assert not (root / "dist" / "quarantine.json").exists()          # unverified claims stay out of exports by default
    assert json.loads((root / "dist" / "packets" / "jane-doe.json").read_text())["quarantined"] == []
    man = export(root, root / "dist", log=lambda *_: None, store=store, cfg={"privacy": {"export_quarantine": True}})
    assert man["quarantine"]["exported"] and json.loads((root / "dist" / "quarantine.json").read_text())["claims"]
    bundle = json.loads((root / "dist" / "subjects" / "jane-doe.json").read_text())
    assert set(bundle["records"]) >= {"statement", "mention", "article", "connection"}
    assert all(rec["evidence"] != "unverified" for k in bundle["records"] for rec in bundle["records"][k])
    assert json.loads((root / "dist" / "targets.json").read_text())[0]["record_counts"] == {"agenda_item": 1}
    packet = json.loads((root / "dist" / "packets" / "jane-doe.json").read_text())
    assert packet["sources"] and all(s["url"] and "opened_at" in s for s in packet["sources"])
    assert packet["quarantined"][0]["status"] == "unverified" and packet["counts"]["mentions"] == 1
