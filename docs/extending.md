# Extending OpenCrawl

Everything that knows about a particular kind of website lives behind one of four extension points. A project can
add its own without touching the core; the civic module is built the same way.

## 1. Adapters: structured systems behind a portal

An adapter reads a system's own API (or the read-only endpoints its web page loads) instead of scraping pages.
It is a module in `opencrawl/adapters/` (or anywhere on your path) that exposes:

```python
NAME = "widgetportal"                       # the portal name; a source may carry it as a key (widgetportal: client)

def detect(src, home_html, hints) -> str | None:
    """Return the client / endpoint id when `src` is on this platform, else None.
    Called with the source dict, the home page's HTML (may be "") and the API hints opencrawl.extract found."""

def probe(fetcher, src, client) -> int:
    """How many items the API answers with (0 = not working). May raise; the probe is timed and recorded."""

def crawl(fetcher, src, client, since, modified_since, ctx, limits):
    """Yield opencrawl.store.Record objects (and opencrawl.channels.Checkpoint objects for resumable backfills).
    modified_since is the last successful run (None on a full crawl); ctx.cursors holds saved checkpoints;
    ctx.frontier is the Store (url_seen / fresh / mark_url) so documents are fetched once."""

def shard_key(src) -> str | None:
    """The host all of this adapter's traffic goes to, so --shard keeps it on one runner; None = the source's own host."""

import sys
from opencrawl.adapters import register
register(sys.modules[__name__])
```

Then, so targets expand into it and `opencrawl seed` recognizes its links:

```python
from opencrawl.targets import PORTAL_PATTERNS, PORTAL_SOURCES
PORTAL_PATTERNS["widgetportal"] = re.compile(r"https?://([a-z0-9-]+)\.widgetportal\.example", re.I)
PORTAL_SOURCES["widgetportal"] = lambda target, base, url: {**base, "id": f"{target['id']}:widgetportal", "url": url,
                                                             "kind": "meeting", "channel": "api",
                                                             "endpoint": f"widgetportal:{url}"}
```

Import your module somewhere that runs before a crawl (a project `sitecustomize.py`, or a tiny package listed in
`settings.yaml derivers:` — any import registers it). `opencrawl/adapters/civicclerk.py` is the smallest built-in
adapter to copy from.

Every request goes through the shared `Fetcher`: robots.txt, per-host spacing, back-off, the block list and the
run's time budget all apply to adapters too. Cite the API resource you actually read (or a link the API returned)
as each record's `url`; never build page URLs from a pattern.

## 2. Derivers: structured records from crawled pages

A deriver turns records of one kind into records of another, incrementally (it only sees records changed since
its last pass, and re-running is safe because derived records have natural keys):

```python
from opencrawl.derive import Deriver, register
from opencrawl.store import Record

def _derive(src: dict, context: dict):
    """src is a stored record (dict); yield new Records. context has targets, root, cfg."""
    if "quarterly report" not in (src.get("title") or "").lower():
        return
    yield Record(src["source_id"], "report", src["url"], title=src["title"], text=src["text"][:2000],
                 published=src.get("published"), channel="derived", meta={"period": "Q3"})

register(Deriver("reports", "kind IN ('page','document') AND source_id NOT LIKE 'subj:%'", _derive))
```

List it in `settings.yaml`:

```yaml
derivers: [opencrawl.civic.press, myproject.reports]
```

`opencrawl derive --only reports --full` re-derives everything once while you are developing it.

## 3. Page extractors: extra records from every HTML page

`opencrawl.channels.PAGE_EXTRACTORS` is a list of callables `(fetcher, source_id, src, result, html) -> iterable of
Record`, called for every HTML page the crawler stores. The transcripts module uses it to pick up caption files and
video players linked from meeting pages. Exceptions are swallowed so an extractor can never break a crawl.

## 4. Seed inspectors and website lookups

`opencrawl.seed.INSPECTORS` is a list of callables `(fetcher, home_html, home_url, out)` run after a target's home page
is read; add keys to `out` (`portals`, `feeds`, anything) and they land in `state/discovered.json`. The civic module
adds an elections-office finder this way. `opencrawl.seed.FOLLOW_LINKS` is the regex of link texts worth opening from
the home page to find portals and feeds.

A target can also say where its website should be looked up: `wikidata: Q...` (the item's official-website claim),
`wikipedia: https://en.wikipedia.org/wiki/...` (its infobox), or nothing (a Wikipedia search by name plus
`search_hint`). The civic module adds a one-query lookup by Census FIPS code for a whole state.

## Record shape

```python
Record(source_id, kind, url, title="", text="", markdown="", published=None, channel="", meta={}, target="", evidence="")
```

- `kind` is any string; the built-in modules use page, document, feed_item, post, article, ocr, transcript, video,
  ordinance, matter, meeting, agenda_item, vote, official, statement, mention, social_mention, connection, lead...
- `target` is the id of the target the record belongs to ("" for standalone sources).
- `evidence` is primary / reported / unverified / lead (see the README); the scheduler fills `primary` for crawled
  records, the research module grades its own.
- The natural key is `source_id | kind | url | id-from-meta`; put the system's own ids (`EventItemId`, `guid`,
  `video_id`, `record_id`...) in `meta` so several records on one URL stay distinct. `opencrawl.store.ID_FIELDS`
  lists the keys it looks at.
- `meta` is free-form JSON; keep it small (counts, ids, a few fields), not a copy of the text.
