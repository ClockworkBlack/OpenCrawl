"""Incremental delta fetching, payload trimming, CivicClerk adapter, pool sizing — offline."""
import json

import pytest

from opencrawl import channels
from opencrawl.channels import _date10, _sitemap_entries
from opencrawl.adapters.civicclerk import CivicClerk
from opencrawl.adapters.legistar import Legistar


class FakeResult:
    def __init__(self, status=200, body=b"", ctype="application/json", url="http://x"):
        self.status, self.content, self.content_type, self.url = status, body, ctype, url
        self.not_modified = False

    @property
    def text(self):
        return self.content.decode()

    def json(self):
        return json.loads(self.content)


class StubFetcher:
    """Records every get() and answers via a routing function the test supplies."""
    def __init__(self, route):
        self.route = route
        self.calls = []
        self.pool_maxsize = 10

    def get(self, url, params=None, headers=None, use_cache=True):
        self.calls.append({"url": url, "params": params or {}, "use_cache": use_cache})
        return self.route(url, params or {})

    def set_pool(self, n):
        self.pool_maxsize = max(10, n)


# --------------------------------------------------------------------------- Legistar
def test_legistar_full_vs_delta_filters_and_select():
    def route(url, params):
        if url.endswith("/matters"):
            # one page then empty
            return FakeResult(body=b"[]") if params.get("$skip") else FakeResult(
                body=json.dumps([{"MatterId": 1, "MatterGuid": "g", "MatterTitle": "An ordinance about parks",
                                  "MatterTypeName": "Ordinance", "MatterIntroDate": "2026-01-02"}]).encode())
        return FakeResult(body=b"[]")

    lg = Legistar(StubFetcher(route), "rifle")
    full = list(lg.matters("2016-01-01"))
    assert full and full[0].kind == "ordinance"

    stub = StubFetcher(route)
    lg2 = Legistar(stub, "rifle")
    list(lg2.matters("2016-01-01", modified_since="2026-11-01T00:00:00"))
    first = stub.calls[0]["params"]
    assert "$select" in first and "MatterLastModifiedUtc" in first["$select"]   # trimmed payload
    assert "2026-11-01" in first["$filter"] and "MatterLastModifiedUtc ge" in first["$filter"]  # delta floor


def test_legistar_meetings_delta_adds_modified_filter():
    seen = {}

    def route(url, params):
        if url.endswith("/events"):
            seen["filter"] = params.get("$filter", "")
            return FakeResult(body=b"[]")
        return FakeResult(body=b"[]")

    list(Legistar(StubFetcher(route), "x").meetings("2016-01-01"))
    assert "EventLastModifiedUtc" not in seen["filter"]          # full run: date floor only
    list(Legistar(StubFetcher(route), "x").meetings("2016-01-01", modified_since="2026-10-01T00:00:00"))
    assert "EventDate ge" in seen["filter"] and "EventLastModifiedUtc ge" in seen["filter"]  # delta: both


# --------------------------------------------------------------------------- CivicClerk
def test_civicclerk_meetings_and_files():
    events = {"value": [{"id": 42, "eventName": "City Council", "startDateTime": "2026-10-20T18:00:00Z",
                         "categoryName": "Council", "publishedFiles": [
                             {"fileId": 7, "name": "Agenda", "url": "https://x.portal.civicclerk.com/file/7"},
                             {"fileId": 8, "name": "Minutes"}]}]}

    def route(url, params):
        if url.endswith("/Events") and "$skip" in params and params["$skip"]:
            return FakeResult(body=b'{"value": []}')
        if url.endswith("/Events"):
            return FakeResult(body=json.dumps(events).encode())
        if url.endswith("/file/7"):
            return FakeResult(body=b"<html><body><main><p>" + b"Agenda item one. " * 20 + b"</p></main></body></html>",
                              ctype="text/html", url=url)
        return FakeResult(body=b'{"value": []}')

    stub = StubFetcher(route)
    recs = list(CivicClerk(stub, "testcity").meetings("2016-01-01", modified_since="2026-10-01"))
    kinds = [r.kind for r in recs]
    assert kinds.count("meeting") == 1 and kinds.count("document") == 1  # only the file that was opened
    meeting = next(r for r in recs if r.kind == "meeting")
    assert meeting.meta["event_id"] == 42 and meeting.published.startswith("2026-10-20")
    # file with an explicit url keeps it; the one without falls back to a stream URL
    docs = [r for r in recs if r.kind == "document"]
    assert docs[0].url.endswith("/file/7") and docs[0].meta["opened"] and "Agenda item" in docs[0].text
    assert not any("GetMeetingFileStream" in c["url"] for c in stub.calls)  # never pattern-built
    assert "2026-10-01" in stub.calls[0]["params"]["$filter"]   # date floor applied


def test_civicclerk_probe():
    stub = StubFetcher(lambda u, p: FakeResult(body=b'{"value": [1,2,3]}'))
    assert CivicClerk(stub, "x").probe() == 3


# --------------------------------------------------------------------------- feeds / wp / sitemap
def test_wp_json_uses_modified_after_on_delta():
    stub = StubFetcher(lambda u, p: FakeResult(body=b"[]"))
    list(channels.crawl(stub, {"id": "s", "url": "http://x"}, {"channel": "wp_json", "endpoint": "http://x/wp-json/wp/v2/posts"},
                        {}, modified_since="2026-09-01T00:00:00"))
    assert "modified_after" in stub.calls[0]["params"] and stub.calls[0]["use_cache"] is False
    stub2 = StubFetcher(lambda u, p: FakeResult(body=b"[]"))
    list(channels.crawl(stub2, {"id": "s", "url": "http://x"}, {"channel": "wp_json", "endpoint": "http://x/wp-json/wp/v2/posts"}, {}))
    assert "after" in stub2.calls[0]["params"] and "modified_after" not in stub2.calls[0]["params"]


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Old</title><link>http://x/old</link><guid>o</guid><pubDate>Mon, 02 Jan 2023 00:00:00 GMT</pubDate></item>
<item><title>New</title><link>http://x/new</link><guid>n</guid><pubDate>Fri, 30 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_feed_delta_skips_old_items():
    stub = StubFetcher(lambda u, p: FakeResult(body=RSS, ctype="application/rss+xml"))
    recs = list(channels.crawl(stub, {"id": "s", "url": "http://x", "follow_feed_links": False},
                               {"channel": "feed", "endpoint": "http://x/feed"}, {}, modified_since="2026-01-01"))
    assert [r.title for r in recs] == ["New"]


SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>http://x/a</loc><lastmod>2022-05-01</lastmod></url>
<url><loc>http://x/b</loc><lastmod>2026-10-15</lastmod></url>
<url><loc>http://x/c</loc></url>
</urlset>"""


def test_sitemap_entries_and_lastmod_cutoff():
    entries = _sitemap_entries(SITEMAP)
    assert entries == [("http://x/a", "2022-05-01"), ("http://x/b", "2026-10-15"), ("http://x/c", None)]

    fetched = []

    def route(url, params):
        if url.endswith("/sitemap.xml"):
            return FakeResult(body=SITEMAP, ctype="application/xml")
        fetched.append(url)
        return FakeResult(body=b"<html><body><main><p>" + b"content " * 40 + b"</p></main></body></html>", ctype="text/html", url=url)

    list(channels.crawl(StubFetcher(route), {"id": "s", "url": "http://x"},
                        {"channel": "sitemap", "endpoint": "http://x/sitemap.xml"}, {}, modified_since="2026-01-01"))
    assert "http://x/a" not in fetched          # older than cutoff: skipped
    assert "http://x/b" in fetched               # newer: fetched
    assert "http://x/c" in fetched               # no lastmod: fetched (can't prove unchanged)


def test_date10():
    assert _date10("2026-10-15T18:00:00Z") == "2026-10-15"
    assert _date10("Fri, 30 Oct 2026 00:00:00 GMT") == "2026-10-30"   # RSS pubDate
    assert _date10("not a date") is None
    assert _date10(None) is None


# --------------------------------------------------------------------------- pool sizing
def test_fetcher_pool_sizes_to_workers(tmp_path):
    import requests

    from opencrawl.fetch import Fetcher

    s = requests.Session()
    f = Fetcher("T/1", tmp_path / "state", session=s, sleep=lambda _: None, pool_maxsize=8)
    assert f.pool_maxsize == 10  # floor
    f.set_pool(40)
    assert f.pool_maxsize == 40
    adapter = s.get_adapter("https://example.gov")
    assert adapter._pool_maxsize == 40
