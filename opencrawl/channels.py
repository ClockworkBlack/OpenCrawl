"""Channel auto-detection and crawling.

For each source: find every way to get its data (official API, WordPress JSON,
RSS/Atom, sitemap, plain HTML, headless browser), time each, and use the
fastest working structured channel. The choice is cached in
state/channels.json, re-checked every `recheck_days`, or at once on failure.

Efficiency rules applied while crawling (all need a `CrawlContext` with a
frontier, i.e. the Store; without one everything is fetched, as in tests):
- documents (PDF, Word, CivicPlus DocumentCenter/AgendaCenter files) are
  fetched once; they don't change after publication
- HTML pages deeper than the listing pages are revisited at most every
  `page_revisit_days`; listing pages (depth 0-1) every run
- links that match `crawl.hot_links` (settings.yaml) are crawled before everything
  else and `crawl.cold_links` are skipped, so the page budget goes where the
  records are; the defaults below suit organization and government sites
- a target's own website gets a coverage crawl (sitemap or HTML) on top of a
  news feed, so a feed never stands in for the whole site
- structured systems (Legistar, CivicClerk, BoardDocs...) are read through the
  adapters in opencrawl.adapters, tried only on sources that look like them
- long structured backfills yield Checkpoints; the scheduler stores them as
  cursors so an interrupted first run resumes instead of starting over
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from lxml import etree

from . import extract
from .blocked import Blocked
from .feeds import looks_like_feed, parse_feed
from .fetch import Disallowed, Fetcher, OutOfTime
from .store import Record

STRUCTURED = ("api", "wp_json", "feed")
PRIORITY = {"api": 0, "wp_json": 1, "feed": 2, "sitemap": 3, "html": 4, "browser": 5}
MIN_PAGE_CHARS = 200  # an HTML page with less extracted text than this is treated as empty (likely client-rendered)

DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|rtf|txt|csv)(?:$|[?#])", re.I)
DOC_PATH = re.compile(r"/(DocumentCenter/View|AgendaCenter/ViewFile|Archive\.aspx\?ADID|ArchiveCenter/ViewFile|"
                      r"GetMeetingFileStream|ViewFile\.aspx|MetaViewer\.php|View\.ashx)", re.I)
DEFAULT_HOT = (r"agenda|minute|ordinance|resolution|council|commission|meeting|board|mayor|clerk|code-of|municipal-code|"
               r"zoning|public-hearing|budget|election|result|qualif|candidate|runoff|referendum|ballot|appoint|press|news|"
               r"release|statement|report|publication|policy|transcript|video|document|archive|AgendaCenter|DocumentCenter|"
               r"campaign|disclosure|contribution|ethics|filing")
DEFAULT_COLD = (r"calendar|event|parks?-and-rec|facility|login|signin|register|search|sitemap|privacy|accessib|employment|"
                r"careers|jobs|pay-|bill-pay|translate|facebook|twitter|instagram|youtube|linkedin|mailto:|tel:|cart|checkout|"
                r"\.(jpg|jpeg|png|gif|svg|webp|mp4|mp3|zip|css|js)$")
HOT = re.compile(DEFAULT_HOT, re.I)
COLD = re.compile(DEFAULT_COLD, re.I)


def configure(crawl_cfg: dict | None) -> None:
    """Apply settings.yaml `crawl:` (hot_links / cold_links regexes; extra_hot / extra_cold alternations)."""
    global HOT, COLD
    c = crawl_cfg or {}
    hot, cold = c.get("hot_links") or DEFAULT_HOT, c.get("cold_links") or DEFAULT_COLD
    if c.get("extra_hot"):
        hot = f"{hot}|{c['extra_hot']}"
    if c.get("extra_cold"):
        cold = f"{cold}|{c['extra_cold']}"
    HOT, COLD = re.compile(hot, re.I), re.compile(cold, re.I)


@dataclass
class Checkpoint:
    """Yielded by a crawl when a backfill window is complete: resume from `value` next time."""
    stream: str
    value: str


@dataclass
class CrawlContext:
    frontier: object | None = None          # Store (url_seen / fresh / mark_url); None = fetch everything
    cursors: dict = field(default_factory=dict)
    pages_ok: bool = True                   # inside the off-peak window: page crawling allowed
    leaf_ttl_days: float = 3650             # documents: effectively once
    page_revisit_days: float = 7            # HTML pages deeper than depth 1
    probes: list = field(default_factory=list)   # cached probes for this source (for the coverage crawl)
    stats: dict = field(default_factory=lambda: {"fetched": 0, "skipped_fresh": 0})


@dataclass
class Probe:
    channel: str
    ok: bool
    latency: float = 0.0
    items: int = 0
    endpoint: str = ""
    note: str = ""

    @property
    def per_item(self) -> float:
        return self.latency / max(1, self.items)


def choose(probes: list[Probe]) -> Probe | None:
    """Fastest working structured channel; else best unstructured fallback by priority."""
    ok = [p for p in probes if p.ok and p.items > 0]
    structured = [p for p in ok if p.channel in STRUCTURED]
    if structured:
        return min(structured, key=lambda p: (p.per_item, PRIORITY[p.channel]))
    rest = [p for p in ok if p.channel not in STRUCTURED]
    return min(rest, key=lambda p: PRIORITY[p.channel]) if rest else None


class ChannelCache:
    def __init__(self, path: Path, recheck_days: int = 7):
        self.path = Path(path)
        self.recheck = dt.timedelta(days=recheck_days)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._lock = threading.Lock()

    def get(self, source_id: str) -> dict | None:
        hit = self.data.get(source_id)
        if not hit:
            return None
        when = dt.datetime.fromisoformat(hit["checked_at"])
        if dt.datetime.now(dt.timezone.utc) - when > self.recheck:
            return None
        return hit

    def put(self, source_id: str, chosen: Probe, probes: list[Probe]):
        self.data[source_id] = {  # dict assignment is atomic; the file write is locked
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "chosen": asdict(chosen),
            "probes": [asdict(p) for p in probes],
        }
        self.save()

    def invalidate(self, source_id: str):
        if self.data.pop(source_id, None) is not None:
            self.save()

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)


# --------------------------------------------------------------------------- probing
def _timed(fn):
    t0 = time.monotonic()
    try:
        res = fn()
        return res, time.monotonic() - t0, None
    except Blocked:
        raise  # the whole host refused us; no point probing further
    except Disallowed as exc:
        return None, time.monotonic() - t0, f"robots.txt disallows {exc}"
    except Exception as exc:  # noqa: BLE001 — a failed probe is just a failed probe
        return None, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"[:200]


def fingerprint(html: str) -> set[str]:
    """Which CMS / platform a page comes from, so probing only tries endpoints that platform has."""
    h = html[:400000].lower()
    fp = set()
    if "/wp-content/" in h or "wp-json" in h or 'content="wordpress' in h:
        fp.add("wordpress")
    if "civicplus" in h or "/agendacenter" in h or "rssfeed.aspx" in h or "/documentcenter/" in h:
        fp.add("civicplus")
    if 'content="drupal' in h or "drupal-settings-json" in h or "/sites/default/files/" in h:
        fp.add("drupal")
    if "granicus" in h:
        fp.add("granicus")
    if "revize" in h:
        fp.add("revize")
    if h.count("<script") > 8 and ('id="root"' in h or "ng-app" in h or "__next_data__" in h or 'id="app"' in h):
        fp.add("spa")
    return fp


FINGERPRINT_FEEDS = {
    "wordpress": ["/feed/"],
    "drupal": ["/rss.xml"],
    "civicplus": ["/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml", "/RSSFeed.aspx?ModID=65&CID=All-0"],
}


def probe_source(fetcher: Fetcher, src: dict) -> list[Probe]:
    """Probe the channels this source can plausibly have. Typically 2-4 requests, not a dozen blind guesses:
    feeds come from the page's own <link rel=alternate> tags or its platform's known feed endpoints, the
    WordPress API is tried only on WordPress sites, sitemaps come from robots.txt."""
    base = src["url"].rstrip("/")
    probes: list[Probe] = []

    home_html, fp = "", set()
    res, lat, err = _timed(lambda: fetcher.get(src["url"]))
    if res is not None and res.status == 200 and "html" in res.content_type:
        home_html = res.text
        fp = fingerprint(home_html)
        _, md = extract.html_to_markdown(home_html, res.url)
        # link-heavy home pages with little prose are normal; only an app shell (script-built page) with
        # almost no text counts as unusable as plain HTML
        ok = bool(md) and not ("spa" in fp and len(md) < MIN_PAGE_CHARS)
        probes.append(Probe("html", ok, lat, 1 if ok else 0, src["url"], "" if ok else "no text without a browser"))
    else:
        probes.append(Probe("html", False, lat, 0, src["url"], err or f"HTTP {getattr(res, 'status', '?')}"))

    hints = extract.find_api_hints(home_html, src["url"]) if home_html else {}
    from .adapters import ADAPTERS

    for name, ad in ADAPTERS.items():
        try:
            client = ad.detect(src, home_html, hints)
        except Exception:  # noqa: BLE001 — a detector must never break probing
            client = None
        if not client:
            continue
        n, lat, err = _timed(lambda ad=ad, client=client: ad.probe(fetcher, src, client))
        probes.append(Probe("api", bool(n), lat, n or 0, f"{name}:{client}", err or ""))

    if "wordpress" in fp or hints.get("wp_json"):
        wp_posts = (hints.get("wp_json") or base + "/wp-json/").rstrip("/") + "/wp/v2/posts"
        res, lat, err = _timed(lambda: fetcher.get(wp_posts, params={"per_page": 10}))
        if res is not None and res.status == 200 and res.content_type.endswith("json"):
            try:
                n = len(res.json())
            except Exception:  # noqa: BLE001
                n = 0
            probes.append(Probe("wp_json", n > 0, lat, n, wp_posts))

    candidates = list(src.get("feeds") or [])
    if home_html:
        candidates += extract.find_feed_links(home_html, src["url"])
    for name in sorted(fp):
        candidates += [urljoin(base + "/", p.lstrip("/")) for p in FINGERPRINT_FEEDS.get(name, [])]
    best_feed = None
    for url in list(dict.fromkeys(candidates))[:6]:
        res, lat, err = _timed(lambda u=url: fetcher.get(u))
        if res is None or res.status != 200 or not looks_like_feed(res.content, res.content_type):
            continue
        n = len(parse_feed(res.content))
        p = Probe("feed", n > 0, lat, n, url)
        if n and (best_feed is None or p.per_item < best_feed.per_item):
            best_feed = p
        if not src.get("feeds") and best_feed:
            break  # auto-detected; one good feed is enough
    if best_feed:
        probes.append(best_feed)

    sitemaps = fetcher.robots.sitemaps(src["url"]) or [base + "/sitemap.xml"]
    res, lat, err = _timed(lambda: fetcher.get(sitemaps[0]))
    if res is not None and res.status == 200 and b"<loc" in res.content[:200000]:
        n = len(_sitemap_entries(res.content))
        probes.append(Probe("sitemap", n > 0, lat, n, sitemaps[0]))

    if src.get("render") == "browser" or ("spa" in fp and not any(p.ok for p in probes if p.channel == "html")):
        probes.append(Probe("browser", True, 0.0, 1, src["url"], "configured" if src.get("render") else "client-rendered"))
    return probes


# --------------------------------------------------------------------------- helpers
def _date10(value: str | None) -> str | None:
    """YYYY-MM-DD from an ISO date or an RFC 822 feed date ("Fri, 30 Oct 2026 ..."); None if unparseable."""
    if not value:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if m:
        return m.group(0)
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value.strip()).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _sitemap_urls(content: bytes) -> list[str]:
    return [loc for loc, _ in _sitemap_entries(content)]


def _sitemap_entries(content: bytes) -> list[tuple[str, str | None]]:
    """(loc, lastmod) for each <url>/<sitemap> entry; lastmod is None when absent."""
    root = etree.fromstring(content, etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    if root is None:
        return []
    out = []
    for el in root.iter():
        if not (isinstance(el.tag, str) and el.tag.endswith("loc") and el.text):
            continue
        parent = el.getparent()
        lastmod = None
        if parent is not None:
            for sib in parent:
                if isinstance(sib.tag, str) and sib.tag.endswith("lastmod") and sib.text:
                    lastmod = sib.text.strip()
                    break
        out.append((el.text.strip(), lastmod))
    return out


def is_document_url(url: str) -> bool:
    return bool(DOC_EXT.search(url) or DOC_PATH.search(url))


def doc_type(url: str, title: str = "") -> str:
    s = f"{url} {title}".lower()
    for t, pat in (("finance_report", r"campaign[ _-]?(?:contribution|finance|disclosure)|contribution[ _-]?disclosure|disclosure[ _-]?report|"
                                      r"disclosure[ _-]?report|financial[ _-]?disclosure"),
                   ("minutes", r"minute"), ("agenda", r"agenda"), ("ordinance", r"ordinance"), ("resolution", r"resolution"),
                   ("results", r"result|canvass|returns|tally|summary report"), ("qualifying", r"qualif"),
                   ("sample_ballot", r"sample ballot|specimen ballot"), ("referendum", r"referendum|ballot question|ballot issue|question"),
                   ("budget", r"budget"), ("packet", r"packet"), ("transcript", r"transcript|caption")):
        if re.search(pat, s):
            return t
    return "document"


def _filter_urls(urls: list[str], src: dict) -> list[str]:
    inc = src.get("include", [])
    exc = src.get("exclude", [])
    out = []
    for u in urls:
        lu = u.lower()
        if inc and not any(s.lower() in lu for s in inc):
            continue
        if any(s.lower() in lu for s in exc):
            continue
        out.append(u)
    return out


def _skip_fresh(ctx: CrawlContext | None, url: str, depth: int | None = None) -> bool:
    """True if the frontier says this URL was fetched recently enough to skip."""
    if not ctx or not ctx.frontier:
        return False
    if is_document_url(url):
        ttl = ctx.leaf_ttl_days
    elif depth is not None and depth >= 2:
        ttl = ctx.page_revisit_days
    else:
        return False
    if ctx.frontier.fresh(url, ttl):
        ctx.stats["skipped_fresh"] += 1
        return True
    return False


def _mark(ctx: CrawlContext | None, sid: str, r) -> None:
    if ctx:
        ctx.stats["fetched"] += 1
        if ctx.frontier:
            ctx.frontier.mark_url(r.url, r.status, sid, r.content_type, r.content if r.status == 200 else None)


def _doc_record(r, sid: str, src: dict, title: str = "") -> Record | None:
    """A fetched non-HTML document as a record (PDFs parsed; scanned pages OCR'd)."""
    if r.content_type == "application/pdf" or r.content[:5] == b"%PDF-":
        from .ingest import pdf_to_record

        kind = "ordinance" if src.get("kind") == "ordinance" else "document"
        rec = pdf_to_record(r.content, source_id=sid, kind=kind, url=r.url, title=title)
        rec.meta["doc_type"] = doc_type(r.url, rec.title)
        rec.meta["opened"] = True
        return rec
    return None


PAGE_EXTRACTORS: list = []   # callables(fetcher, sid, src, result, html) -> iterable of extra Records


def page_extras(fetcher: Fetcher, sid: str, src: dict, r, html: str):
    for fn in PAGE_EXTRACTORS:
        try:
            yield from fn(fetcher, sid, src, r, html)
        except (Disallowed, Blocked):
            continue
        except Exception:  # noqa: BLE001 — an extractor must not break the crawl
            continue


def _page_record(fetcher: Fetcher, sid: str, url: str, src: dict, ctx: CrawlContext | None = None,
                 depth: int | None = None) -> Record | None:
    if _skip_fresh(ctx, url, depth):
        return None
    try:
        r = fetcher.get(url)
    except Disallowed:
        return None
    _mark(ctx, sid, r)
    if r.status != 200:
        return None
    doc = _doc_record(r, sid, src)
    if doc or "html" not in r.content_type:
        return doc
    title, md = extract.html_to_markdown(r.text, r.url)
    if not md:
        return None
    rec = Record(sid, src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html", meta={"opened": True},
                 published=extract.page_date(r.text, md))
    rec.extras = list(page_extras(fetcher, sid, src, r, r.text))  # transcripts etc. linked from this page
    return rec


def with_extras(rec):
    """Yield a record and anything an extractor found on its page."""
    if rec is None:
        return
    yield rec
    for x in getattr(rec, "extras", None) or []:
        yield x


# --------------------------------------------------------------------------- crawling
def crawl(fetcher: Fetcher, src: dict, chosen: dict, limits: dict, modified_since: str | None = None,
          ctx: CrawlContext | None = None) -> Iterator[Record | Checkpoint]:
    """Yield records (and Checkpoints) from the chosen channel.

    modified_since (the source's last successful run) turns a re-run into a
    delta: structured channels ask the server for only what changed, and feeds
    and sitemaps drop items older than it. None = full crawl (first run), which
    resumes from ctx.cursors when an earlier full crawl was interrupted.
    """
    ch, endpoint = chosen["channel"], chosen["endpoint"]
    sid = src["id"]
    since = src.get("since", limits.get("since", "2016-01-01"))
    max_pages = src.get("max_pages", limits.get("max_pages", 200))
    cutoff = _date10(modified_since)
    ctx = ctx or CrawlContext()

    if ch == "api" and endpoint.startswith("youtube:"):
        from .transcripts import youtube_records

        yield from youtube_records(fetcher, sid, src.get("target", ""), endpoint.split(":", 1)[1], since,
                                   target_name=src.get("target_name", ""), basis=src.get("youtube_basis", ""))
        return

    if ch == "api":
        from .adapters import ADAPTERS

        name, _, client = endpoint.partition(":")
        ad = ADAPTERS.get(name)
        if ad is None:
            raise RuntimeError(f"no adapter named {name!r} for endpoint {endpoint}")
        yield from ad.crawl(fetcher, src, client, since, modified_since, ctx, limits)
        return

    if ch == "wp_json":
        page = 1
        while page <= max_pages:
            params = {"per_page": 50, "page": page, "orderby": "modified", "order": "desc"}
            params["modified_after" if modified_since else "after"] = (modified_since or since + "T00:00:00")
            r = fetcher.get(endpoint, params=params, use_cache=not modified_since)
            if r.status != 200:
                break
            rows = r.json()
            if not rows:
                break
            for p in rows:
                _, md = extract.html_to_markdown(p.get("content", {}).get("rendered", ""), p.get("link", ""))
                yield Record(sid, "post" if src.get("kind") == "campaign" else src.get("kind", "page"), p.get("link", ""),
                             title=extract.soup_of(p.get("title", {}).get("rendered", "")).get_text(),
                             text=md, markdown=md, published=p.get("date"), channel="wp_json",
                             meta={"wp_id": p.get("id"), "opened": True})
            page += 1
        yield from _coverage(fetcher, src, ctx, max_pages)
        return

    if ch == "feed":
        r = fetcher.get(endpoint, use_cache=not modified_since)
        for item in parse_feed(r.content):
            if cutoff and _date10(item.get("published")) and _date10(item["published"]) < cutoff:
                continue  # already had this on the last run
            if src.get("follow_feed_links", True) and item["link"]:
                if _skip_fresh(ctx, item["link"], depth=2):
                    continue
                page = _page_record(fetcher, sid, item["link"], src, ctx)
                if page:
                    page.channel = "feed"
                    page.published = page.published or item["published"]
                    page.meta["guid"] = item["id"]
                    yield from with_extras(page)
                    continue
            yield Record(sid, "feed_item", item["link"], title=item["title"], text=item["summary"],
                         published=item["published"], channel="feed", meta={"feed": endpoint, "guid": item["id"]})
        yield from _coverage(fetcher, src, ctx, max_pages)
        return

    if ch == "sitemap":
        yield from _sitemap_crawl(fetcher, src, endpoint, ctx, max_pages, cutoff, use_cache=not modified_since)
        return

    if ch == "browser":
        yield from _browser_crawl(fetcher, src, max_pages, ctx)
        return

    yield from _html_crawl(fetcher, src, max_pages, ctx)


def _coverage(fetcher: Fetcher, src: dict, ctx: CrawlContext, max_pages: int) -> Iterator[Record]:
    """Official websites: a news feed or posts API covers news, not council pages and documents.
    Follow up with the sitemap (or an HTML crawl) — only inside the page-crawling window."""
    if not src.get("coverage") or not ctx.pages_ok:
        return
    sm = next((p for p in ctx.probes if p.get("channel") == "sitemap" and p.get("ok")), None)
    if sm:
        yield from _sitemap_crawl(fetcher, src, sm["endpoint"], ctx, max_pages, None, use_cache=True)
    else:
        yield from _html_crawl(fetcher, src, max_pages, ctx)


def _sitemap_crawl(fetcher: Fetcher, src: dict, endpoint: str, ctx: CrawlContext, max_pages: int, cutoff: str | None,
                   use_cache: bool = True) -> Iterator[Record]:
    sid = src["id"]
    r = fetcher.get(endpoint, use_cache=use_cache)
    entries = _sitemap_entries(r.content)
    if entries and all(loc.endswith(".xml") or "sitemap" in loc.lower() for loc, _ in entries[:5]):
        nested = []
        for loc, lastmod in entries[:50]:
            if cutoff and lastmod and lastmod[:10] < cutoff:
                continue  # whole child sitemap unchanged since last run
            try:
                nested += _sitemap_entries(fetcher.get(loc, use_cache=use_cache).content)
            except (Disallowed, Blocked):
                pass
        entries = nested
    entries = [(loc, lm) for loc, lm in entries if not (cutoff and lm and lm[:10] < cutoff)]
    # relevant pages first, so max_pages is spent where the records are
    urls = sorted(_filter_urls([loc for loc, _ in entries], src), key=lambda u: (not HOT.search(u), bool(COLD.search(u))))
    n = 0
    for u in urls:
        if n >= max_pages:
            break
        if _skip_fresh(ctx, u, depth=2):
            continue
        rec = _page_record(fetcher, sid, u, src, ctx)
        n += 1
        yield from with_extras(rec)


def _html_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    """Polite breadth-first crawl inside the site, hot links (agendas, minutes, ordinances...) first."""
    ctx = ctx or CrawlContext()
    start = src["url"]
    host = urlsplit(start).netloc.lower().removeprefix("www.")
    hot, cold = deque([(start, 0)]), deque()
    queued = {start}
    depth_max = src.get("depth", 3)
    count = 0
    while (hot or cold) and count < max_pages:
        url, depth = (hot or cold).popleft()
        if _skip_fresh(ctx, url, depth):
            continue
        try:
            r = fetcher.get(url)
        except Disallowed:
            continue
        _mark(ctx, src["id"], r)
        if r.status != 200:
            continue
        count += 1
        doc = _doc_record(r, src["id"], src)
        if doc:
            yield doc
            continue
        if "html" not in r.content_type:
            continue
        title, md = extract.html_to_markdown(r.text, r.url)
        if md and (not src.get("include") or _filter_urls([r.url], {"include": src["include"]})):
            yield Record(src["id"], src.get("kind", "page"), r.url, title=title, text=md, markdown=md, channel="html",
                         meta={"opened": True})
            yield from page_extras(fetcher, src["id"], src, r, r.text)
        if depth >= depth_max:
            continue
        for link in extract.links(r.text, r.url):
            lh = urlsplit(link).netloc.lower().removeprefix("www.")
            if lh != host or link in queued or not _filter_urls([link], {"exclude": src.get("exclude", [])}):
                continue
            if COLD.search(link) and not is_document_url(link):
                continue
            queued.add(link)
            (hot if HOT.search(link) or is_document_url(link) else cold).append((link, depth + 1))


def _browser_crawl(fetcher: Fetcher, src: dict, max_pages: int, ctx: CrawlContext | None = None) -> Iterator[Record]:
    """Headless Chromium for JS-rendered sites (code libraries, single-page sites), through browser.session (every
    connection guarded). Same robots/block-list/rate-limit/frontier rules as everything else."""
    from . import browser

    ctx = ctx or CrawlContext()
    start = src["url"]
    host = urlsplit(start).netloc
    selector = src.get("content_selector")
    link_pattern = src.get("follow")  # substring a link must contain to be followed
    seen, queue, count = set(), deque([(start, 0)]), 0
    with browser.session(fetcher, block=("media", "font", "image")) as bctx:
        page = bctx.new_page()
        while queue and count < max_pages:
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if not fetcher.allowed(url) or _skip_fresh(ctx, url, depth):
                continue
            try:
                resp = browser.goto(page, url, fetcher)
            except (OutOfTime, Blocked):
                raise
            except Disallowed:
                continue
            except Exception:  # noqa: BLE001 — navigation error, timeout
                fetcher.limiter.record(url, 599, 0.0)
                continue
            status = resp.status if resp else 0
            final = page.url
            if final != url and urlsplit(final).netloc != host:
                continue      # the page moved to another site: not this source's content
            if ctx.frontier:
                ctx.frontier.mark_url(url, status, src["id"], "text/html", page.content().encode() if status == 200 else None)
            if status != 200:
                continue
            count += 1
            html = page.inner_html(selector) if selector and page.query_selector(selector) else page.content()
            title, md = extract.html_to_markdown(f"<html><head><title>{_html_escape(page.title())}</title></head>"
                                                 f"<body>{html}</body></html>", url)
            if md:
                yield Record(src["id"], src.get("kind", "page"), url, title=title, text=md, markdown=md, channel="browser",
                             meta={"opened": True})
            for link in extract.links(page.content(), url):
                if urlsplit(link).netloc == host and (not link_pattern or link_pattern in link) and link not in seen:
                    queue.append((link, depth + 1))


def _html_escape(s: str) -> str:
    import html as _h

    return _h.escape(s or "", quote=False)
