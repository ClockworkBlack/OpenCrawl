"""CivicClerk adapter (portal.civicclerk.com).

CivicClerk exposes a public OData feed at https://<client>.api.civicclerk.com/v1/
behind the portal UI. This reads Events (meetings) and their published files
(agendas, minutes, packets), newest first, with an optional modified_since/date
floor so re-runs only pull new meetings.

Field names vary a little between CivicClerk versions, so parsing is tolerant:
each value is looked up under several candidate keys and missing ones are left
blank. The adapter is purely additive — every target also has its website
(`:site`) source — so if a deployment's API shape differs, nothing is lost; the
source simply falls back on the next run.
"""
from __future__ import annotations

from typing import Iterator

from ..fetch import Fetcher
from ..store import Record

API = "https://{client}.api.civicclerk.com/v1"
PAGE = 200


def _first(d: dict, *keys, default=""):
    for k in keys:
        for cand in (k, k[0].lower() + k[1:], k[0].upper() + k[1:]):
            if cand in d and d[cand] not in (None, ""):
                return d[cand]
    return default


class CivicClerk:
    def __init__(self, fetcher: Fetcher, client: str):
        self.f = fetcher
        self.client = client
        self.base = API.format(client=client)

    def _get(self, path: str, params: dict | None = None):
        r = self.f.get(self.base + path, params=params, headers={"Accept": "application/json"})
        if r.status != 200:
            raise RuntimeError(f"CivicClerk {self.client}{path}: HTTP {r.status}")
        return r.json()

    def probe(self) -> int:
        data = self._get("/Events", {"$top": 5})
        return len(data.get("value", data if isinstance(data, list) else []))

    def _rows(self, path: str, flt: str | None, order: str, limit: int | None) -> Iterator[dict]:
        skip, n = 0, 0
        while True:
            params = {"$top": PAGE, "$skip": skip, "$orderby": order}
            if flt:
                params["$filter"] = flt
            data = self._get(path, params)
            rows = data.get("value", data) if isinstance(data, dict) else data
            if not rows:
                return
            for row in rows:
                yield row
                n += 1
                if limit and n >= limit:
                    return
            if len(rows) < PAGE:
                return
            skip += PAGE

    def meetings(self, since: str, limit: int | None = None, modified_since: str | None = None) -> Iterator[Record]:
        floor = (modified_since or since)[:10]
        flt = f"startDateTime ge {floor}T00:00:00Z"
        for e in self._rows("/Events", flt, "startDateTime desc", limit):
            eid = _first(e, "id", "eventId")
            start = _first(e, "startDateTime", "eventDate", "date")
            name = _first(e, "eventName", "name", "categoryName", "title")
            # cite the API resource that was read (or a link the API itself gives), never a built page URL
            page = _first(e, "eventUrl", "url", "portalUrl") or (f"{self.base}/Events({eid})" if eid else f"{self.base}/Events")
            yield Record(source_id=self.client, kind="meeting", channel="api", url=page,
                         title=f"{name} {str(start)[:10]}".strip(), published=start or None,
                         text=" · ".join(str(x) for x in (name, str(start)[:10], _first(e, "location", "eventLocation")) if x),
                         meta={"event_id": eid, "category": _first(e, "categoryName", "category"),
                               "location": _first(e, "location", "eventLocation"), "opened": True,
                               "provider": "civicclerk"})
            files = e.get("publishedFiles") or e.get("eventFiles") or e.get("files") or []
            if not files and eid:
                try:
                    sub = self._get(f"/Events({eid})/PublishedFiles")
                    files = sub.get("value", sub) if isinstance(sub, dict) else sub
                except Exception:  # noqa: BLE001 — not every deployment exposes the sub-collection
                    files = []
            for fdoc in files or []:
                rec = self._open_file(fdoc, name, start, eid)
                if rec:
                    yield rec

    def _open_file(self, fdoc: dict, name: str, start, eid):
        """Fetch a published file and record what was actually opened.

        Only URLs the API itself gives are used (no pattern-built links), and a
        file becomes a record only after it was opened (HTTP 200); its text is
        extracted so the record is citeable and searchable."""
        from ..blocked import Blocked
        from ..fetch import Disallowed

        url = _first(fdoc, "url", "downloadUrl", "fileUrl")
        fname = _first(fdoc, "name", "fileName", "type", default="document")
        if not url:
            return None
        try:
            r = self.f.get(url)
        except (Disallowed, Blocked):
            return None
        if r.status != 200:
            return None
        meta = {"event_id": eid, "file_id": _first(fdoc, "fileId", "id"), "file_name": fname, "opened": True}
        title = f"{name}: {fname}".strip()
        if r.content_type == "application/pdf" or r.content[:5] == b"%PDF-":
            from ..ingest import pdf_to_record

            rec = pdf_to_record(r.content, source_id=self.client, kind="document", url=r.url, title=title, meta=meta)
            rec.published = start or None
            return rec
        from ..extract import html_to_markdown

        _, md = html_to_markdown(r.text, r.url) if "html" in r.content_type else ("", r.text[:200000])
        return Record(source_id=self.client, kind="document", channel="api", url=r.url, title=title,
                      text=md, markdown=md, published=start or None, meta=meta)


# ----------------------------------------------------------------------------- adapter interface
NAME = "civicclerk"


def detect(src: dict, home_html: str, hints: dict) -> str | None:
    from urllib.parse import urlsplit

    client = src.get("civicclerk")
    if not client and ".portal.civicclerk.com" in src["url"]:
        client = urlsplit(src["url"]).netloc.split(".")[0]
    return client or None


def probe(fetcher: Fetcher, src: dict, client: str) -> int:
    return CivicClerk(fetcher, client).probe()


def crawl(fetcher: Fetcher, src: dict, client: str, since: str, modified_since: str | None, ctx, limits: dict):
    yield from CivicClerk(fetcher, client).meetings(since, src.get("max_items"), modified_since=modified_since)


def shard_key(src: dict) -> str | None:
    return f"{src['civicclerk']}.api.civicclerk.com" if src.get("civicclerk") else None


import sys as _sys  # noqa: E402

from . import register as _register  # noqa: E402

_register(_sys.modules[__name__])
