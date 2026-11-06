"""Offline tests: a local HTTP server stands in for city sites."""
import datetime as dt
import http.server
import threading
from pathlib import Path

import pytest
import requests

from opencrawl import channels
from opencrawl.extract import html_to_markdown
from opencrawl.feeds import parse_feed
from opencrawl.fetch import Disallowed, Fetcher
from opencrawl.polite import RateLimiter, Schedule
from opencrawl.store import Store
from opencrawl.verified_posts import Registry, handle_of, platform_of

SITE = {
    "/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /private\nCrawl-delay: 0\n"),
    "/": ("text/html", b"""<html><head><title>City of Testville</title>
        <link rel="alternate" type="application/rss+xml" href="/news.rss"></head>
        <body><nav>menu</nav><main><h1>City Council</h1>
        <p>The council meets on the first Monday of each month at City Hall.</p>
        <p>Sec. 2-14. Quorum.</p><p>A majority of the council constitutes a quorum for business.</p>
        <a href="/ordinances">Ordinances</a> <a href="/private/x">secret</a>
        <table><tr><th>Ordinance</th><th>Status</th></tr><tr><td>2026-01</td><td>Adopted</td></tr></table>
        </main><footer>footer</footer></body></html>"""),
    "/ordinances": ("text/html", b"<html><body><main><h2>Ordinances</h2><p>" + b"Ordinance text. " * 30 + b"</p></main></body></html>"),
    "/news.rss": ("application/rss+xml", b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
        <item><title>Council adopts budget</title><link>http://HOST/ordinances</link><guid>1</guid>
        <pubDate>Mon, 02 Nov 2026 19:00:00 GMT</pubDate><description>Adopted 5-2.</description></item>
        </channel></rss>"""),
    "/private/x": ("text/html", b"<html><body>nope</body></html>"),
    "/campaign": ("text/html", b"""<html><body><main><p>Follow Jane on
        <a href="https://bsky.app/profile/janedoe.bsky.social">Bluesky</a> and
        <a href="https://www.instagram.com/janedoe_example/">Instagram</a>.</p></main></body></html>"""),
}


class Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        Handler.hits.append(self.path)
        path = self.path.split("?")[0]
        if path not in SITE:
            self.send_response(404); self.end_headers(); return
        ctype, body = SITE[path]
        body = body.replace(b"HOST", self.headers["Host"].encode())
        etag = '"%d"' % hash(body)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False  # bypass any proxy for localhost
    return Fetcher("TestCrawler/0.1", tmp_path / "state", min_interval=0, session=s, sleep=lambda s: None)


def test_robots_blocks_private(server, fetcher):
    assert fetcher.allowed(server + "/")
    with pytest.raises(Disallowed):
        fetcher.get(server + "/private/x")


def test_conditional_get_uses_cache(server, fetcher):
    a = fetcher.get(server + "/ordinances")
    b = fetcher.get(server + "/ordinances")
    assert a.status == 200 and b.not_modified and a.content == b.content


def test_markdown_extraction():
    _, md = html_to_markdown(SITE["/"][1].decode(), "http://x")
    assert "# City Council" in md
    assert "### Sec. 2-14. Quorum." in md
    assert "| Ordinance | Status |" in md
    assert "menu" not in md and "footer" not in md


def test_feed_parse():
    items = parse_feed(SITE["/news.rss"][1])
    assert items[0]["title"] == "Council adopts budget"


def test_discovery_prefers_feed_and_crawls(server, fetcher, tmp_path):
    src = {"id": "testville", "url": server + "/", "kind": "council"}
    probes = channels.probe_source(fetcher, src)
    chosen = channels.choose(probes)
    assert chosen.channel == "feed"
    recs = list(channels.crawl(fetcher, src, {"channel": chosen.channel, "endpoint": chosen.endpoint}, {}))
    assert recs and "Ordinance text" in recs[0].markdown
    store = Store(tmp_path / "data")
    assert store.add(recs[0]) and not store.add(recs[0])  # dedupe


def test_html_crawl_respects_robots(server, fetcher):
    src = {"id": "t", "url": server + "/", "depth": 2}
    recs = list(channels.crawl(fetcher, src, {"channel": "html", "endpoint": server + "/"}, {}))
    urls = {r.url for r in recs}
    assert server + "/ordinances" in urls
    assert not any("/private" in u for u in urls)


def test_choose_fastest_structured():
    p = [channels.Probe("feed", True, 1.0, 10), channels.Probe("api", True, 0.2, 5), channels.Probe("html", True, 0.1, 1)]
    assert channels.choose(p).channel == "api"
    p = [channels.Probe("feed", True, 0.1, 10), channels.Probe("api", True, 2.0, 5)]
    assert channels.choose(p).channel == "feed"
    p = [channels.Probe("api", False, 0.1, 0), channels.Probe("sitemap", True, 1, 9), channels.Probe("html", True, 1, 1)]
    assert channels.choose(p).channel == "sitemap"


def test_rate_limiter_backs_off():
    rl = RateLimiter(1.0, sleep=lambda s: None)
    rl.record("http://a/x", 200, 0.1)
    rl.record("http://a/x", 429, 0.1)
    rl.record("http://a/x", 503, 0.1)
    assert rl.interval("http://a") == 4.0
    for _ in range(20):
        rl.record("http://a/x", 200, 0.1)
    assert rl.interval("http://a") == 1.0


def test_schedule_windows():
    s = Schedule.from_config({"timezone": "America/New_York", "windows": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "23:00", "end": "06:00"}]})
    et = s.tz
    assert s.is_good_time(dt.datetime(2026, 11, 5, 23, 30, tzinfo=et))   # Thu night
    assert s.is_good_time(dt.datetime(2026, 11, 6, 3, 0, tzinfo=et))     # Fri early (Thu's window)
    assert not s.is_good_time(dt.datetime(2026, 11, 6, 12, 0, tzinfo=et))
    assert not s.is_good_time(dt.datetime(2026, 11, 8, 3, 0, tzinfo=et))  # Sun early (Sat not listed)
    assert s.next_good_time(dt.datetime(2026, 11, 6, 12, 0, tzinfo=et)).hour == 23


def test_social_url_parsing():
    assert platform_of("https://x.com/janedoe/status/1") == "x"
    assert handle_of("https://twitter.com/JaneDoe/status/1") == "janedoe"
    assert handle_of("https://bsky.app/profile/janedoe.bsky.social/post/abc") == "janedoe.bsky.social"
    assert handle_of("https://www.instagram.com/p/XYZ/") is None
    assert handle_of("https://www.youtube.com/@JaneForGA") == "@janeforga"


def test_verified_posts_flow(server, fetcher, tmp_path):
    reg = Registry(tmp_path / "verified")
    reg.add_account("Jane Doe", "https://bsky.app/profile/janedoe.bsky.social", server + "/campaign", "Council D3", "Testville")
    reg.add_account("Jane Doe", "https://x.com/notjane", server + "/campaign")
    reg.add_post("https://bsky.app/profile/janedoe.bsky.social/post/abc", "Vote yes on the parks ordinance")
    assert reg.posts()[0]["status"] == "pending"           # account not yet verified
    rows = {r["id"]: r for r in reg.verify_accounts(fetcher)}
    assert rows["bluesky:janedoe.bsky.social"]["status"] == "verified"
    assert rows["x:notjane"]["status"] == "unverified"      # campaign page doesn't link it
    assert reg.posts()[0]["status"] == "verified"
    out = tmp_path / "v.csv"
    assert reg.export_csv(out) == 1
