"""SQLite store, frontier, resumable backfill, sharding, subject import, batched news, packets — offline."""
import datetime as dt
import http.server
import json
import sqlite3
import threading
from pathlib import Path

import pytest
import requests
import yaml

from opencrawl import channels
from opencrawl.research.subjects import Researcher, load_subjects
from opencrawl.research.packets import build_packet, norm_url
from opencrawl.export import council_alignment
from opencrawl.fetch import Fetcher
from opencrawl.names import excerpts, name_regex, name_variants
from opencrawl.shard import in_shard, merge_delta, merge_state, parse_shard, shard_key, write_delta
from opencrawl.adapters.legistar import Legistar, _year_windows
from opencrawl.store import Record, Store
from opencrawl.topics import tag

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- store
def test_store_upsert_versions_and_search(tmp_path):
    s = Store(tmp_path / "data")
    r = Record("x:site", "page", "http://x/council", title="City Council", text="The council adopted the budget.")
    assert s.add(r) is True and s.add(r) is False                     # unchanged: no write
    r2 = Record("x:site", "page", "http://x/council", title="City Council", text="The council tabled the zoning item.")
    assert s.add(r2) is True                                          # changed: same row, new version
    rows = s.read("x:site", "page")
    assert len(rows) == 1 and rows[0]["version"] == 2 and rows[0]["id"] == r.stable_id
    s.add(Record("x:site", "agenda_item", "http://x/m/1", title="Item A", meta={"EventItemId": 1}))
    s.add(Record("x:site", "agenda_item", "http://x/m/1", title="Item B", meta={"EventItemId": 2}))
    assert len(s.read("x:site", "agenda_item")) == 2                  # same URL, different natural ids
    s.flush()
    hits = s.search(Store.fts_phrase("zoning item"))
    assert len(hits) == 1 and "zoning" in hits[0]["snippet"]
    assert s.search(Store.fts_phrase("adopted the budget")) == []     # old version no longer indexed
    assert s.counts("kind") == {"page": 1, "agenda_item": 2} and s.total() == 3
    s.close()


def test_store_frontier_cursors_and_concurrent_reader(tmp_path):
    s = Store(tmp_path / "data")
    s.mark_url("http://x/a.pdf", 200, "x", "application/pdf", b"%PDF-1")
    assert s.fresh("http://x/a.pdf", 1) and not s.fresh("http://x/b.pdf", 1)
    s.mark_url("http://x/c", 404, "x")
    assert not s.fresh("http://x/c", 1)
    s.set_cursor("x:legistar", "events", "2019-01-01")
    assert s.cursors("x:legistar") == {"events": "2019-01-01"}
    s.add(Record("x", "page", "http://x/1", text="pending write"))   # open write transaction
    reader = Store(tmp_path / "data")                                # second process opens it: must not block
    assert reader.cursors("x:legistar") == {"events": "2019-01-01"}
    reader.close()
    s.clear_cursors("x:legistar")
    assert s.cursors("x:legistar") == {}
    s.close()


class FakeResult:
    def __init__(self, body, status=200, ctype="application/json", url="http://x"):
        self.status, self.content, self.content_type, self.url = status, body, ctype, url
        self.not_modified = False

    @property
    def text(self):
        return self.content.decode()

    def json(self):
        return json.loads(self.content)


class Stub:
    def __init__(self, route):
        self.route, self.calls = route, []

    def get(self, url, params=None, headers=None, use_cache=True):
        self.calls.append((url, dict(params or {})))
        return self.route(url, params or {})


def test_year_windows():
    w = _year_windows("2024-03-01", "2026-06-01")
    assert w == [("2024-03-01", "2025-01-01"), ("2025-01-01", "2026-01-01"), ("2026-01-01", "2026-06-01")]


def test_legistar_backfill_checkpoints_and_resume():
    def route(url, params):
        if url.endswith("/events") and not params.get("$skip"):
            y = params["$filter"].split("datetime'")[1][:4]
            return FakeResult(json.dumps([{"EventId": int(y), "EventBodyName": "Council", "EventDate": f"{y}-05-01T00:00:00",
                                           "EventInSiteURL": f"https://x.legistar.com/MeetingDetail.aspx?ID={y}"}]).encode())
        return FakeResult(b"[]")

    stub = Stub(route)
    items = list(Legistar(stub, "x").meetings("2016-01-01", with_votes=False))
    cps = [i for i in items if isinstance(i, channels.Checkpoint)]
    meetings = [i for i in items if not isinstance(i, channels.Checkpoint)]
    assert cps[0].value == "2017-01-01" and len(meetings) == len(cps)            # one checkpoint per year window
    assert meetings[0].url.startswith("https://x.legistar.com/MeetingDetail")   # link the API gave, not a built one
    # resume: a cursor skips the windows already done
    stub2 = Stub(route)
    list(Legistar(stub2, "x").meetings("2016-01-01", with_votes=False, cursors={"events": "2024-01-01"}))
    first_filter = [p for u, p in stub2.calls if u.endswith("/events")][0]["$filter"]
    assert "2024-01-01" in first_filter and "2016" not in first_filter


def test_legistar_delta_includes_recent_lookback():
    stub = Stub(lambda u, p: FakeResult(b"[]"))
    list(Legistar(stub, "x").meetings("2016-01-01", modified_since="2026-11-01T00:00:00", lookback_days=45))
    flt = stub.calls[0][1]["$filter"]
    assert "EventLastModifiedUtc ge" in flt and " or EventDate ge" in flt


def test_matter_urls_are_api_resources():
    stub = Stub(lambda u, p: FakeResult(b"[]") if p.get("$skip") else FakeResult(
        json.dumps([{"MatterId": 7, "MatterTitle": "An ordinance", "MatterTypeName": "Ordinance"}]).encode()))
    rec = next(r for r in Legistar(stub, "x").matters("2026-01-01") if not isinstance(r, channels.Checkpoint))
    assert rec.url == "https://webapi.legistar.com/v1/x/matters/7" and rec.kind == "ordinance"


# ----------------------------------------------------------------------------- frontier skip on re-crawl
PAGES = {
    "/robots.txt": (b"User-agent: *\nAllow: /\n", "text/plain"),
    "/": (b"<html><body><main><h1>City</h1><p>" + b"Welcome to the city. " * 20 +
          b"</p><a href='/agendas'>Agendas</a> <a href='/parks'>Parks</a></main></body></html>", "text/html"),
    "/agendas": (b"<html><body><main><p>" + b"Council agendas and minutes. " * 20 +
                 b"</p><a href='/files/minutes-2026-10.pdf'>Minutes</a></main></body></html>", "text/html"),
    "/parks": (b"<html><body><main><p>" + b"Parks info. " * 30 + b"</p></main></body></html>", "text/html"),
}


def _pdf_bytes():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(buf, "PDF")
    return buf.getvalue()


class Site(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        Site.hits.append(self.path)
        if self.path.endswith(".pdf"):
            body, ctype = _pdf_bytes(), "application/pdf"
        elif self.path in PAGES:
            body, ctype = PAGES[self.path]
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def test_html_crawl_hot_first_and_frontier_skips_documents(site, fetcher, tmp_path):
    store = Store(tmp_path / "data")
    src = {"id": "t:site", "url": site + "/", "kind": "page", "depth": 3}
    ctx = channels.CrawlContext(frontier=store)
    Site.hits.clear()
    recs = list(channels.crawl(fetcher, src, {"channel": "html", "endpoint": site + "/"}, {}, ctx=ctx))
    order = [h for h in Site.hits if h != "/robots.txt"]
    assert order.index("/agendas") < order.index("/parks")              # hot links first
    assert any(r.kind == "document" and r.meta["doc_type"] == "minutes" for r in recs)
    Site.hits.clear()
    ctx2 = channels.CrawlContext(frontier=store)
    list(channels.crawl(fetcher, src, {"channel": "html", "endpoint": site + "/"}, {}, ctx=ctx2))
    assert not any(h.endswith(".pdf") for h in Site.hits)                # document not fetched again
    assert ctx2.stats["skipped_fresh"] >= 1
    store.close()


def test_scheduler_checkpoints_become_cursors(tmp_path, fetcher, monkeypatch):
    from opencrawl.scheduler import Progress, run_sources

    calls = {"n": 0}

    def fake_crawl(f, src, chosen, limits, modified_since=None, ctx=None):
        calls["cursors"] = dict(ctx.cursors)
        yield Record(src["id"], "meeting", "http://x/1", title="m1", meta={"EventId": 1})
        yield channels.Checkpoint("events", "2018-01-01")
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("network dropped")  # interrupted mid-backfill
        yield Record(src["id"], "meeting", "http://x/2", title="m2", meta={"EventId": 2})

    monkeypatch.setattr(channels, "crawl", fake_crawl)
    store, prog = Store(tmp_path / "data"), Progress(tmp_path / "p.json")
    cache = channels.ChannelCache(tmp_path / "c.json")
    src = {"id": "x:legistar", "url": "https://x.legistar.com/", "channel": "api", "endpoint": "legistar:x", "tier": "a"}
    cfg = {"schedule": {"windows": []}}
    run_sources(cfg, [src], fetcher, store, cache, prog, 1e12, log=lambda *_: None)
    assert store.cursors("x:legistar") == {"events": "2018-01-01"} and prog.get("x:legistar")["status"].startswith("error")
    run_sources(cfg, [src], fetcher, store, cache, prog, 1e12, log=lambda *_: None)
    assert calls["cursors"] == {"events": "2018-01-01"}                 # second run resumed from the cursor
    assert prog.get("x:legistar")["status"] == "ok" and store.cursors("x:legistar") == {}  # done: cursor cleared
    store.close()


# ----------------------------------------------------------------------------- sharding
def test_shards_partition_by_host():
    srcs = [{"id": f"j{i}:site", "url": f"https://city{i}.gov/"} for i in range(200)]
    srcs += [{"id": f"l{i}:legistar", "url": f"https://l{i}.legistar.com/", "legistar": f"l{i}"} for i in range(20)]
    shards = [[s["id"] for s in srcs if in_shard(s, (i, 4))] for i in range(4)]
    assert sum(map(len, shards)) == len(srcs) and len(set(sum(shards, []))) == len(srcs)   # exact partition
    assert min(map(len, shards)) > 20                                                     # roughly balanced
    leg = {i for i in range(4) for s in shards[i] if s.endswith(":legistar")}
    assert len(leg) == 1                                    # every Legistar source on the one shared API host's shard
    assert shard_key({"id": "a", "url": "https://www.x.gov/a"}) == "x.gov" and parse_shard("2/4") == (2, 4)
    with pytest.raises(ValueError):
        parse_shard("4/4")


def test_delta_and_merge_roundtrip(tmp_path):
    base = Store(tmp_path / "base.db")
    base.add(Record("a:site", "page", "http://a/1", text="one"))
    base.add(Record("a:site", "page", "http://a/2", text="two"))
    base.close()
    import time
    time.sleep(1.1)  # timestamps have 1 s resolution; rows from the same second as `since` would also (harmlessly) ship
    since = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    shard = tmp_path / "shard.db"
    shard.write_bytes((tmp_path / "base.db").read_bytes())
    sh = Store(shard)
    sh.add(Record("a:site", "page", "http://a/2", text="two, revised"))  # changed
    sh.add(Record("b:site", "page", "http://b/1", text="new"))           # new
    sh.mark_url("http://b/1", 200, "b:site", "text/html", b"new")
    sh.set_cursor("b:legistar", "events", "2020-01-01")
    info = write_delta(sh, since, tmp_path / "delta.db")
    sh.close()
    assert info["records"] == 2 and info["urls"] == 1 and info["cursors"] == 1
    base = Store(tmp_path / "base.db")
    merge_delta(base, tmp_path / "delta.db")
    assert {r["text"] for r in base.query("1")} == {"one", "two, revised", "new"} and base.total() == 3
    assert base.search(Store.fts_phrase("revised"))                      # search index follows the merge
    assert base.cursors("b:legistar") == {"events": "2020-01-01"} and base.fresh("http://b/1", 1)
    merge_delta(base, tmp_path / "delta.db")                             # idempotent
    assert base.total() == 3
    base.close()


def test_merge_state(tmp_path):
    a, b, t = tmp_path / "a", tmp_path / "b", tmp_path / "t"
    for d in (a, b):
        d.mkdir()
    (a / "progress.json").write_text(json.dumps({"x": {"last_run": "2026-11-01"}, "y": {"last_run": "2026-11-01"}}))
    (b / "progress.json").write_text(json.dumps({"x": {"last_run": "2026-11-05", "status": "ok"}}))
    (a / "blocked.json").write_text(json.dumps({"h": {"last_seen": "2026-11-01", "count": 2, "sources": ["s1"], "urls": ["u1"]}}))
    (b / "blocked.json").write_text(json.dumps({"h": {"last_seen": "2026-11-02", "count": 1, "sources": ["s2"], "urls": ["u2"]}}))
    merge_state(t, [a, b])
    p = json.loads((t / "progress.json").read_text())
    bl = json.loads((t / "blocked.json").read_text())["h"]
    assert p["x"]["status"] == "ok" and "y" in p and bl["count"] == 2 and bl["sources"] == ["s1", "s2"]


# ----------------------------------------------------------------------------- names, topics, alignment
def test_name_variants_and_matching():
    v = name_variants('Robert "Bob" Example Jr.')
    assert "Robert Example" in v and "Bob Example" in v and not any("Jr" in x for x in v)
    assert name_variants("DOE, JANE A")[0] == "Jane A Doe" and "Jane Doe" in name_variants("DOE, JANE A")
    assert name_variants("TAYLOR CALDER (NORTH COUNTY)") == ["Taylor Calder"]
    assert name_variants("Cher") == []                                   # never a single name
    rx = name_regex(name_variants("Robin A. Ashby"))
    assert rx.search("said Robin A. Ashby on Monday") and rx.search("ROBIN ASHBY") and not rx.search("Robin Ashbys")
    ex = excerpts("x " * 300 + "Commissioner Robin Ashby voted no. " + "y " * 300, rx)
    assert len(ex) == 1 and "Robin Ashby voted no" in ex[0] and ex[0].startswith("…")


def test_topic_tags():
    t = tag("An ordinance to rezone 12 acres for affordable apartments", "")
    assert "Governance and policy" in t and "Housing and land use" in t
    assert tag("Approval of the FY2027 budget", "") == ["Budget and finance"]
    assert tag("", "") == []


def test_council_alignment():
    def v(*pairs):
        return {"meta": {"votes": [{"person": p, "vote": x} for p, x in pairs]}}
    votes = [v(("A", "Yea"), ("B", "Yea"), ("C", "Nay")) for _ in range(4)] + [v(("A", "Yea"), ("B", "Yea"), ("C", "Yea"))]
    al = council_alignment(votes)
    pab = next(p for p in al["pairs"] if (p["a"], p["b"]) == ("A", "B"))
    pac = next(p for p in al["pairs"] if (p["a"], p["b"]) == ("A", "C"))
    assert al["votes_counted"] == 4 and pab["pct"] == 100.0 and pac["pct"] == 0.0   # unanimous vote ignored


# ----------------------------------------------------------------------------- subjects import (synthetic data only)
SUBJECTS_CSV = """id,name,aliases,kind,target,targets,places,websites,social,priority,office,races
robin-ashby,Robin Ashby,Robin L. Ashby,person,springfield,springfield;co-baca,Baca County,https://robinforcouncil.example/,https://bsky.app/profile/robin.example,true,Council Member,
sam-state,Sam State,,person,,,,,,,State Senator,
pat-private,Pat Private,,person,,,,,,,,
"""


def _project(tmp: Path) -> Path:
    root = tmp / "proj"
    (root / "config").mkdir(parents=True)
    for f in ("outlets.yaml", "topics.yaml"):
        (root / "config" / f).write_text((ROOT / "config" / f).read_text())
    (root / "config" / "jurisdictions.yaml").write_text((ROOT / "opencrawl" / "examples" / "colorado-civic" / "config" / "jurisdictions.yaml").read_text())
    (root / "subjects.csv").write_text(SUBJECTS_CSV)
    from opencrawl.research.importers import import_subjects

    import_subjects(root / "subjects.csv", root, log=lambda *_: None)
    return root


def test_subjects_import_csv_and_merge_with_hand_entries(tmp_path):
    root = _project(tmp_path)
    (root / "config" / "subjects.yaml").write_text(yaml.safe_dump({"subjects": [
        {"id": "robin-ashby", "websites": ["https://www.robinforcouncil.example/about"], "office": "Council Member Ward 1"},
        {"name": "Taylor Calder", "target": "co-baca", "public_role": "county commission candidate"}]}))
    subs = {c["id"]: c for c in load_subjects(root, {})}
    assert set(subs) == {"robin-ashby", "sam-state", "taylor-calder"}       # pat-private: no public role, not researched
    pat = subs["robin-ashby"]
    assert pat["aliases"] == ["Robin L. Ashby"] and pat["targets"] == ["springfield", "co-baca"] and pat["priority"] is True
    assert pat["websites"] == ["https://robinforcouncil.example/", "https://www.robinforcouncil.example/about"]   # union, imported first
    assert pat["office"] == "Council Member Ward 1" and "Robin L. Ashby" in pat["variants"] and pat["places"] == ["Baca County"]
    assert subs["taylor-calder"]["targets"] == ["co-baca"] and subs["sam-state"]["targets"] == []
    from opencrawl.research.importers import import_subjects

    meta = import_subjects(root / "subjects.csv", root, log=lambda *_: None)
    assert meta["subjects"] == 3 and meta["from"] == "subjects.csv"     # the file name only, not its path


def test_votes_and_mentions_from_store_and_packet(tmp_path, fetcher):
    root = _project(tmp_path)
    store = Store(root / "data")
    store.add(Record("springfield:legistar", "vote", "https://x.legistar.com/MeetingDetail.aspx?ID=9", title="Rezoning of 12 acres",
                     target="springfield", meta={"EventItemId": 5, "result": "Pass", "topics": ["Housing and land use"],
                                                 "votes": [{"person": "Robin Ashby", "vote": "Nay"}, {"person": "Other Person", "vote": "Yea"}]}))
    store.add(Record("springfield:agendas", "document", "https://springfield.example/minutes.pdf", title="Minutes",
                     text="Council member Robin Ashby asked about the water rates.", target="springfield"))
    store.add(Record("pueblo:site", "page", "https://pueblo.example/p", title="Other city",
                     text="A different Robin Ashby spoke.", target="pueblo"))   # other target: not attributed
    store.flush()
    cand = {c["id"]: c for c in load_subjects(root, {})}["robin-ashby"]
    r = Researcher(root, {}, fetcher, store, log=lambda *_: None)
    counts = r.research(cand, skip={"posts", "statements", "articles", "leads", "social_mentions", "connections"})
    assert counts == {"mention": 1, "vote": 1}
    votes = store.read("subj:robin-ashby", "vote")
    assert votes[0]["meta"]["vote"] == "Nay" and votes[0]["meta"]["topics"] == ["Housing and land use"]
    recs = {k: store.read("subj:robin-ashby", k) for k in store.kinds("subj:robin-ashby")}
    pk = build_packet(cand, recs, [], {norm_url("https://www.robinforcouncil.example/about")})
    assert pk["subject_id"] == "robin-ashby" and pk["vote_topics"]["Housing and land use"]["no"] == 1
    assert all(s["opened_at"] for s in pk["sources"]) and pk["votes"][0]["refs"] == [0]
    assert pk["targets"] == ["springfield", "co-baca"]
    store.close()


def test_articles_batch_opens_and_verifies(tmp_path, monkeypatch):
    root = _project(tmp_path)
    store = Store(root / "data")
    gdelt = {"articles": [
        {"url": "https://apnews.com/a1", "title": "Springfield council votes", "domain": "apnews.com", "seendate": "20261101T000000Z"},
        {"url": "https://apnews.com/a2", "title": "Robin Ashby in Ohio", "domain": "apnews.com", "seendate": "20261101T000000Z"},
        {"url": "https://paywalled.example/a3", "title": "Robin Ashby profile", "domain": "paywalled.example", "seendate": "20261102"},
        {"url": "https://blog.example/a4", "title": "Unsourced claim about Robin Ashby", "domain": "blog.example", "seendate": "20261103"}]}
    pages = {
        "https://apnews.com/a1": "<html><body><main><p>In Springfield, Baca County, council member Robin Ashby said the "
                                  "budget needs work.</p>" + "<p>filler text here.</p>" * 20 + "</main></body></html>",
        "https://apnews.com/a2": "<html><body><main><p>Robin Ashby of Columbus, Ohio, opened a bakery.</p>"
                                  + "<p>filler</p>" * 20 + "</main></body></html>",
        "https://blog.example/a4": "<html><body><main><p>Word is Robin Ashby of Springfield, Colorado is quitting.</p>"
                                   + "<p>filler</p>" * 20 + "</main></body></html>"}

    class F:
        def get(self, url, params=None, headers=None, use_cache=True):
            if "gdeltproject" in url:
                return FakeResult(json.dumps(gdelt).encode())
            if url in pages:
                return FakeResult(pages[url].encode(), ctype="text/html", url=url)
            return FakeResult(b"paywall", status=403, ctype="text/html", url=url)

    cand = {c["id"]: c for c in load_subjects(root, {})}["robin-ashby"]
    r = Researcher(root, {}, F(), store, log=lambda *_: None)
    counts = r.research(cand, skip={"mentions", "votes", "posts", "statements", "leads", "social_mentions", "connections"})
    arts = store.read("subj:robin-ashby", "article")
    assert [a["url"] for a in arts] == ["https://apnews.com/a1"]          # Ohio namesake rejected (no place match)
    assert arts[0]["evidence"] == "reported" and "budget needs work" in arts[0]["text"]
    leads = store.read("subj:robin-ashby", "lead")
    assert [l["url"] for l in leads] == ["https://paywalled.example/a3"] and leads[0]["evidence"] == "lead"
    r.quarantine.flush()
    assert [x["url"] for x in r.quarantine.all()] == ["https://blog.example/a4"] and r.quarantine.all()[0]["opened"] is True
    assert counts.get("quarantined") == 1
    store.close()


def test_wikidata_lookup_by_article_title():
    from opencrawl.seed import wiki_title, wikidata_websites_by_titles

    assert wiki_title("https://en.wikipedia.org/wiki/Aurora,_Colorado") == "Aurora, Colorado"
    assert wiki_title("https://en.wikipedia.org/wiki/Ca%C3%B1on_City,_Colorado#History") == "Cañon City, Colorado"
    queries = []

    class F:
        def get(self, url, params=None, headers=None, use_cache=True):
            queries.append(params["query"])
            return FakeResult(json.dumps({"results": {"bindings": [
                {"title": {"value": "Aurora, Colorado"}, "web": {"value": "https://www.auroragov.example/"}}]}}).encode())

    out = wikidata_websites_by_titles(F(), ["Aurora, Colorado", "Denver, Colorado", "Aurora, Colorado"])
    assert out == {"Aurora, Colorado": "https://www.auroragov.example/"}
    assert len(queries) == 1 and '"Aurora, Colorado"@en "Denver, Colorado"@en' in queries[0]


def test_api_endpoints_skip_robots_only_for_listed_paths(tmp_path):
    f = Fetcher("T/1", tmp_path / "state", min_interval=0, sleep=lambda _: None,
                api_endpoints=["query.wikidata.org/sparql", "en.wikipedia.org/w/api.php"])
    assert f._is_api_endpoint("https://query.wikidata.org/sparql?query=x")
    assert f._is_api_endpoint("https://en.wikipedia.org/w/api.php?action=parse")
    assert not f._is_api_endpoint("https://en.wikipedia.org/w/index.php?title=x")
    assert not f._is_api_endpoint("https://query.wikidata.org/sparqlx")
    assert not f._is_api_endpoint("https://library.municode.com/co/x")


def test_user_agent_contact(monkeypatch):
    from opencrawl.config import _user_agent
    monkeypatch.delenv("OPENCRAWL_USER_AGENT", raising=False)
    monkeypatch.delenv("OPENCRAWL_CONTACT", raising=False)
    monkeypatch.setenv("ORR_FROM", "records@project.example")   # the records mailbox is never borrowed
    assert _user_agent({"user_agent": "Bot/1 (+https://x)"}) == "Bot/1 (+https://x)"
    monkeypatch.setenv("OPENCRAWL_CONTACT", "crawler@project.example")
    assert _user_agent({"user_agent": "Bot/1 (+https://x)"}) == "Bot/1 (+https://x; crawler@project.example)"
    monkeypatch.delenv("OPENCRAWL_CONTACT")
    assert _user_agent({"user_agent": "Bot/1 (+https://x)", "contact": "bot@project.example"}) == \
        "Bot/1 (+https://x; bot@project.example)"
