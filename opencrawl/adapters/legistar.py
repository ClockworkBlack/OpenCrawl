"""Legistar Web API adapter (webapi.legistar.com) — matters (ordinances, resolutions),
meetings, agenda items, roll-call votes and office records. Structured, fast, and
the preferred channel for any city on Legistar.

Efficiency
- deltas: re-runs ask only for matters/meetings modified since the last run, plus a
  short lookback on recent meetings (minutes and votes are often posted weeks later)
- resumable backfill: a first run walks history in yearly windows, oldest first, and
  yields a Checkpoint after each window, so an interrupted backfill resumes there
- $select trims every response to the fields used (auto-disabled if a server rejects it)

Citations: every record's url is either a link the API itself returned
(EventInSiteURL) or the API resource that was actually read. Nothing is built from
a page-URL pattern.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterator

from ..fetch import Fetcher
from ..store import Record

API = "https://webapi.legistar.com/v1/{client}"
PAGE = 1000

MATTER_SELECT = ("MatterId,MatterGuid,MatterFile,MatterName,MatterTitle,MatterTypeName,MatterStatusName,"
                 "MatterBodyName,MatterIntroDate,MatterAgendaDate,MatterPassedDate,MatterEnactmentDate,"
                 "MatterEnactmentNumber,MatterLastModifiedUtc")
EVENT_SELECT = ("EventId,EventBodyName,EventDate,EventTime,EventLocation,EventAgendaFile,EventMinutesFile,"
                "EventVideoPath,EventAgendaStatusName,EventMinutesStatusName,EventInSiteURL,EventLastModifiedUtc")


def _odate(iso: str) -> str:
    return f"datetime'{iso[:19] if 'T' in iso else iso + 'T00:00:00'}'"


def _year_windows(start: str, end: str) -> list[tuple[str, str]]:
    """[start, Jan 1 next year), [Jan 1, Jan 1 next) ... up to end (exclusive). ISO dates."""
    out, a = [], start[:10]
    while a < end:
        b = f"{int(a[:4]) + 1}-01-01"
        b = min(b, end)
        out.append((a, b))
        a = b
    return out


class Legistar:
    def __init__(self, fetcher: Fetcher, client: str, token: str | None = None):
        self.f = fetcher
        self.client = client
        self.base = API.format(client=client)
        self.token = token
        self._no_select = False
        self._office_rows: list[dict] | None = None   # /officerecords, read once per run

    def _get(self, path: str, params: dict | None = None):
        params = dict(params or {})
        if self.token:
            params["token"] = self.token
        r = self.f.get(self.base + path, params=params, headers={"Accept": "application/json"})
        if r.status != 200:
            # Legistar explains a 400 in the body (e.g. a client whose API isn't enabled, or a field it lacks)
            detail = re.sub(r"\s+", " ", (r.text or "")[:240]).strip()
            raise RuntimeError(f"Legistar {self.client}{path}: HTTP {r.status}" + (f" ({detail})" if detail else ""))
        return r.json()

    def _paged(self, path: str, flt: str | None = None, order: str | None = None, limit: int | None = None,
               select: str | None = None) -> Iterator[dict]:
        skip, n = 0, 0
        while True:
            params = {"$top": PAGE, "$skip": skip}
            if flt:
                params["$filter"] = flt
            if order:
                params["$orderby"] = order
            if select and not self._no_select:
                params["$select"] = select
            try:
                batch = self._get(path, params)
            except RuntimeError as exc:
                if "$select" in params and "HTTP 400" in str(exc):
                    self._no_select = True  # this deployment rejects $select: fall back to full rows
                    continue
                raise
            if not batch:
                return
            for row in batch:
                yield row
                n += 1
                if limit and n >= limit:
                    return
            if len(batch) < PAGE:
                return
            skip += PAGE

    def probe(self) -> int:
        """Cheap availability check used by channel discovery: returns item count of a tiny page."""
        return len(self._get("/bodies", {"$top": 5}))

    # ------------------------------------------------------------------ matters
    def _matter(self, m: dict) -> Record:
        mtype = (m.get("MatterTypeName") or "").strip()
        return Record(
            source_id=self.client, kind="ordinance" if "ordinance" in mtype.lower() else "matter", channel="api",
            url=f"{self.base}/matters/{m['MatterId']}",
            title=m.get("MatterTitle") or m.get("MatterName") or "",
            text="\n\n".join(x for x in (m.get("MatterName"), m.get("MatterTitle")) if x),
            published=m.get("MatterIntroDate") or m.get("MatterAgendaDate"),
            meta={**{k: m.get(k) for k in (
                "MatterId", "MatterFile", "MatterTypeName", "MatterStatusName", "MatterBodyName",
                "MatterIntroDate", "MatterPassedDate", "MatterEnactmentDate", "MatterEnactmentNumber",
                "MatterLastModifiedUtc")}, "opened": True, "provider": "legistar"},
        )

    def matters(self, since: str, types: list[str] | None = None, limit: int | None = None,
                modified_since: str | None = None, cursors: dict | None = None) -> Iterator[Record]:
        from ..channels import Checkpoint

        def keep(m):
            mtype = (m.get("MatterTypeName") or "").strip()
            return not types or any(t.lower() in mtype.lower() for t in types)

        if modified_since:  # delta: only what changed since the last successful run
            for m in self._paged("/matters", f"MatterLastModifiedUtc ge {_odate(modified_since)}",
                                 "MatterLastModifiedUtc desc", limit, MATTER_SELECT):
                if keep(m):
                    yield self._matter(m)
            return
        # full backfill, oldest window first, resumable
        start = (cursors or {}).get("matters") or since
        end = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for a, b in _year_windows(start, end):
            flt = f"MatterLastModifiedUtc ge {_odate(a)} and MatterLastModifiedUtc lt {_odate(b)}"
            for m in self._paged("/matters", flt, "MatterLastModifiedUtc", limit, MATTER_SELECT):
                if keep(m):
                    yield self._matter(m)
            yield Checkpoint("matters", b)

    # ------------------------------------------------------------------ meetings
    def _meeting_record(self, e: dict, extra: dict | None = None) -> Record:
        eid = e["EventId"]
        page = e.get("EventInSiteURL") or f"{self.base}/events/{eid}"
        return Record(
            source_id=self.client, kind="meeting", channel="api", url=page,
            title=f"{e.get('EventBodyName', '')} {str(e.get('EventDate', ''))[:10]}".strip(),
            text=" · ".join(str(x) for x in (e.get("EventBodyName"), str(e.get("EventDate", ""))[:10], e.get("EventTime"),
                                             e.get("EventLocation")) if x),
            published=e.get("EventDate"),
            meta={**{k: e.get(k) for k in (
                "EventId", "EventBodyName", "EventDate", "EventTime", "EventLocation", "EventAgendaFile",
                "EventMinutesFile", "EventVideoPath", "EventAgendaStatusName", "EventMinutesStatusName",
                "EventLastModifiedUtc")}, "opened": True, "provider": "legistar", **(extra or {})},
        )

    @staticmethod
    def _info(e: dict, it: dict) -> dict:
        """What a vote record needs to know about its agenda item."""
        return {"EventId": e["EventId"], "EventBodyName": e.get("EventBodyName"), "EventDate": e.get("EventDate"),
                "page": e.get("EventInSiteURL") or "", "EventItemId": it["EventItemId"], "title": it.get("EventItemTitle") or "",
                "MatterId": it.get("EventItemMatterId"), "MatterFile": it.get("EventItemMatterFile"),
                "passed": it.get("EventItemPassedFlagName"), "action": it.get("EventItemActionName")}

    def _item_records(self, e: dict, items_seen: dict) -> Iterator[Record]:
        eid = e["EventId"]
        page = e.get("EventInSiteURL") or f"{self.base}/events/{eid}"
        for it in self._get(f"/events/{eid}/eventitems", {"AgendaNote": 1, "MinutesNote": 1}):
            info = self._info(e, it)
            info["page"] = page
            items_seen[int(it["EventItemId"])] = info
            yield Record(
                source_id=self.client, kind="agenda_item", channel="api", url=page,
                title=it.get("EventItemTitle") or "",
                text="\n\n".join(x for x in (it.get("EventItemTitle"), it.get("EventItemAgendaNote"),
                                             it.get("EventItemMinutesNote")) if x),
                published=e.get("EventDate"),
                meta={"EventId": eid, "EventBodyName": e.get("EventBodyName"), "opened": True, "provider": "legistar",
                      "opened_url": f"{self.base}/events/{eid}/eventitems",
                      **{k: it.get(k) for k in (
                          "EventItemId", "EventItemMatterId", "EventItemMatterFile", "EventItemMatterType",
                          "EventItemActionName", "EventItemPassedFlagName", "EventItemTally", "EventItemMover",
                          "EventItemSeconder")}},
            )

    def _vote_record(self, info: dict, votes: list[dict], opened: list[str]) -> Record:
        return Record(
            source_id=self.client, kind="vote", channel="api", url=info["page"] or f"{self.base}/events/{info['EventId']}",
            title=info["title"], text=info["title"], published=info["EventDate"],
            meta={"EventId": info["EventId"], "EventItemId": info["EventItemId"], "EventBodyName": info["EventBodyName"],
                  "MatterId": info["MatterId"], "MatterFile": info["MatterFile"], "result": info["passed"],
                  "action": info["action"], "opened": True, "provider": "legistar", "opened_url": opened[0],
                  "opened_urls": opened[:12], "votes": votes})

    def _per_item_votes(self, info: dict) -> Record | None:
        """One roll call read from /eventitems/{id}/votes (the fallback, and for deployments without office records)."""
        url = f"{self.base}/eventitems/{info['EventItemId']}/votes"
        rows = self._get(f"/eventitems/{info['EventItemId']}/votes")
        if not rows:
            return None
        rows.sort(key=lambda v: (v.get("VoteSort") or 0, v.get("VotePersonName") or ""))
        return self._vote_record(info, [{"person": v.get("VotePersonName"), "person_id": v.get("VotePersonId"),
                                         "vote": v.get("VoteValueName")} for v in rows], [url])

    @staticmethod
    def _stored(store, source_id: str | None, kind: str) -> dict[int, dict]:
        """Records already in the database for this source, by Legistar id (EventId for meetings, EventItemId otherwise)."""
        out: dict[int, dict] = {}
        if store is None or not source_id:
            return out
        idf = "EventId" if kind == "meeting" else "EventItemId"
        for r in store.iter_query("source_id=? AND kind=?", (source_id, kind)):
            v = r["meta"].get(idf)
            if v is not None:
                out[int(v)] = r
        return out

    def person_ids(self) -> dict[int, str]:
        """Everyone with an office record (council members, commissioners, board members, past and present)."""
        if self._office_rows is None:
            self._office_rows = list(self._paged("/officerecords"))
        out = {}
        for p in self._office_rows:
            pid = p.get("OfficeRecordPersonId")
            if pid:
                out[int(pid)] = p.get("OfficeRecordFullName") or ""
        return out

    def sweep_votes(self, since_iso: str | None, items_seen: dict, store=None, source_id: str | None = None,
                    refresh_event=None) -> Iterator[Record]:
        """Roll calls assembled from each office holder's votes (/persons/{id}/votes): one paged call per person
        instead of one call per agenda item. A delta (`since_iso`) asks only for votes recorded or changed since then,
        merges them into the stored roll call, and re-reads the agenda of each affected meeting once (for the
        item's result and action)."""
        persons = self.person_ids()
        if not persons:
            return
        flt = f"VoteLastModifiedUtc ge {_odate(since_iso)}" if since_iso else None
        by_item: dict[int, dict[int, dict]] = {}
        readers: dict[int, list[str]] = {}
        for pid, pname in persons.items():
            try:
                rows = list(self._paged(f"/persons/{pid}/votes", flt))
            except RuntimeError as exc:
                if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                    continue
                raise
            for v in rows:
                iid = v.get("VoteEventItemId")
                if iid is None:
                    continue
                iid = int(iid)
                vp = v.get("VotePersonId") or pid
                by_item.setdefault(iid, {})[int(vp)] = {"person": v.get("VotePersonName") or pname, "person_id": vp,
                                                        "vote": v.get("VoteValueName"), "_sort": v.get("VoteSort") or 0}
                readers.setdefault(iid, [])
                url = f"{self.base}/persons/{pid}/votes"
                if url not in readers[iid]:
                    readers[iid].append(url)
        if not by_item:
            return
        stored_items = None
        stored_votes = self._stored(store, source_id, "vote") if since_iso else {}
        # a delta: the meetings these votes belong to may have new results/actions on their items
        if since_iso and refresh_event is not None:
            missing = [i for i in by_item if i not in items_seen]
            if missing:
                stored_items = self._stored(store, source_id, "agenda_item")
                events = {int(stored_items[i]["meta"]["EventId"]) for i in missing if i in stored_items and stored_items[i]["meta"].get("EventId")}
                for eid in sorted(events):
                    yield from refresh_event(eid)
        for iid, votes in by_item.items():
            info = items_seen.get(iid)
            if info is None:
                if stored_items is None:
                    stored_items = self._stored(store, source_id, "agenda_item")
                r = stored_items.get(iid)
                if r is None:
                    continue  # an item outside the crawl window
                m = r["meta"]
                info = {"EventId": m.get("EventId"), "EventBodyName": m.get("EventBodyName"), "EventDate": r.get("published"),
                        "page": r["url"], "EventItemId": iid, "title": r.get("title") or "", "MatterId": m.get("EventItemMatterId"),
                        "MatterFile": m.get("EventItemMatterFile"), "passed": m.get("EventItemPassedFlagName"),
                        "action": m.get("EventItemActionName")}
            merged = dict(votes)
            old = stored_votes.get(iid)
            if old:  # keep the members whose votes did not change
                for x in old["meta"].get("votes") or []:
                    if x.get("person_id") is not None and int(x["person_id"]) not in merged:
                        merged[int(x["person_id"])] = {**x, "_sort": 99}
            rows = sorted(merged.values(), key=lambda x: (x.get("_sort") or 0, x.get("person") or ""))
            yield self._vote_record(info, [{k: v for k, v in x.items() if k != "_sort"} for x in rows], readers.get(iid) or [""])

    def meetings(self, since: str, with_votes: bool = True, limit: int | None = None,
                 modified_since: str | None = None, cursors: dict | None = None,
                 lookback_days: int = 45, store=None, source_id: str | None = None,
                 votes_mode: str = "person", items_refresh_days: int = 7) -> Iterator[Record]:
        """Meetings, agenda items and roll calls.

        Agenda items: read for every meeting on a full pass; on a delta, for meetings that changed and for recent
        meetings (`lookback_days`) whose items were last read more than `items_refresh_days` ago (minutes notes and
        results arrive weeks after a meeting). Roll calls: from each office holder's votes (`sweep_votes`), or per
        agenda item when `votes_mode` is "item" or the deployment lists no office records."""
        from ..channels import Checkpoint

        today = dt.date.today()
        items_seen: dict[int, dict] = {}
        known = self._stored(store, source_id, "meeting") if (store is not None and modified_since) else {}
        events_by_id: dict[int, dict] = {}

        def one(e: dict, force_items: bool) -> Iterator[Record]:
            eid = int(e["EventId"])
            events_by_id[eid] = e
            prev = known.get(eid)
            prev_meta = prev["meta"] if prev else {}
            changed = not prev or prev_meta.get("EventLastModifiedUtc") != e.get("EventLastModifiedUtc")
            last_items = prev_meta.get("items_fetched") or ""
            stale = not last_items or last_items < (today - dt.timedelta(days=items_refresh_days)).isoformat()
            if force_items or changed or stale:
                yield self._meeting_record(e, {"items_fetched": today.isoformat()})
                yield from self._item_records(e, items_seen)
            else:
                yield self._meeting_record(e, {"items_fetched": last_items})

        def refresh(eid: int) -> Iterator[Record]:
            e = events_by_id.get(eid)
            if e is None:
                try:
                    e = self._get(f"/events/{eid}")
                except RuntimeError:
                    return
            if e and eid not in {int(i["EventId"]) for i in items_seen.values()}:
                yield self._meeting_record(e, {"items_fetched": today.isoformat()})
                yield from self._item_records(e, items_seen)

        if modified_since:
            # changed since the last run, or recent enough that minutes/votes may still be arriving
            recent = (today - dt.timedelta(days=lookback_days)).isoformat()
            flt = (f"EventDate ge {_odate(since)} and (EventLastModifiedUtc ge {_odate(modified_since)} "
                   f"or EventDate ge {_odate(recent)})")
            for e in self._paged("/events", flt, "EventDate desc", limit, EVENT_SELECT):
                yield from one(e, force_items=False)
        else:
            start = (cursors or {}).get("events") or since
            end = (today + dt.timedelta(days=366)).isoformat()  # include scheduled meetings
            for a, b in _year_windows(start, end):
                flt = f"EventDate ge {_odate(a)} and EventDate lt {_odate(b)}"
                for e in self._paged("/events", flt, "EventDate", limit, EVENT_SELECT):
                    yield from one(e, force_items=True)
                yield Checkpoint("events", b)
        if not with_votes:
            return
        swept = False
        if votes_mode == "person":
            try:
                persons = self.person_ids()
            except RuntimeError:
                persons = {}
            if persons:
                swept = True
                yield from self.sweep_votes(modified_since, items_seen, store, source_id, refresh_event=refresh)
        if not swept:  # no office records here: read each passed item's roll call
            for info in list(items_seen.values()):
                if info.get("passed"):
                    rec = self._per_item_votes(info)
                    if rec:
                        yield rec

    # ------------------------------------------------------------------ sponsors
    def sponsors(self, matter_id: int) -> list[dict]:
        """Who sponsored a matter: [{name, person_id, sequence}] from /matters/{id}/sponsors (empty if none)."""
        try:
            rows = self._get(f"/matters/{matter_id}/sponsors")
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return []
            raise
        out = []
        for s in rows or []:
            out.append({"name": s.get("MatterSponsorName"), "person_id": s.get("MatterSponsorNameId"),
                        "sequence": s.get("MatterSponsorSequence")})
        return out

    # ------------------------------------------------------------------ people
    def find_persons(self, last_name: str) -> list[dict]:
        safe = last_name.replace("'", "''")
        return self._get("/persons", {"$filter": f"PersonLastName eq '{safe}'"})

    def person_votes(self, person_id: int, since: str | None = None) -> list[dict]:
        flt = f"VoteLastModifiedUtc ge {_odate(since)}" if since else None
        return list(self._paged(f"/persons/{person_id}/votes", flt))

    def event_item(self, event_item_id: int) -> dict | None:
        try:
            return self._get(f"/eventitems/{event_item_id}")
        except Exception:  # noqa: BLE001 — not every Legistar deployment exposes this endpoint
            return None

    def officials(self) -> Iterator[Record]:
        if self._office_rows is None:
            self._office_rows = list(self._paged("/officerecords"))
        for p in self._office_rows:
            rid = p.get("OfficeRecordId")
            yield Record(
                source_id=self.client, kind="official", channel="api", url=f"{self.base}/officerecords/{rid}",
                title=p.get("OfficeRecordFullName") or "",
                text=" · ".join(str(x) for x in (p.get("OfficeRecordFullName"), p.get("OfficeRecordTitle"),
                                                 p.get("OfficeRecordBodyName")) if x),
                meta={**{k: p.get(k) for k in ("OfficeRecordId", "OfficeRecordPersonId", "OfficeRecordBodyName",
                                               "OfficeRecordTitle", "OfficeRecordStartDate", "OfficeRecordEndDate",
                                               "OfficeRecordMemberType")}, "opened": True, "provider": "legistar"},
            )


SPONSOR_TYPES = ("ordinance", "resolution", "bill", "substitute")


def fill_sponsors(fetcher: Fetcher, store, limit: int = 2000, deadline: float | None = None, log=print,
                  tokens: dict | None = None) -> dict:
    """Resumable: fetch sponsors for stored Legistar ordinances/resolutions that have not been checked
    (or changed since the check). One API call per matter; stops at the deadline. Sponsors land in
    meta.sponsors as [{name, person_id, sequence}] and a `sponsor` relation is recorded for each name."""
    import time

    from ..store import Record

    checked = found = 0
    by_client: dict[str, Legistar] = {}
    rows = store.query("kind IN ('ordinance','matter') AND channel='api' AND meta LIKE '%\"provider\": \"legistar\"%' "
                       "AND meta NOT LIKE '%\"sponsors_modified\"%'")
    for r in rows:
        if deadline and time.monotonic() > deadline:
            break
        if checked >= limit:
            break
        m = r["meta"]
        if not any(t in (m.get("MatterTypeName") or "").lower() for t in SPONSOR_TYPES):
            m["sponsors_modified"] = m.get("MatterLastModifiedUtc") or ""
            m["sponsors"] = []
            store.add(Record(r["source_id"], r["kind"], r["url"], title=r.get("title") or "", text=r.get("text") or "",
                             markdown=r.get("markdown") or "", published=r.get("published"), channel="api",
                             target=r.get("target") or "", evidence=r.get("evidence") or "primary", meta=m,
                             key=r.get("key") or None))
            continue
        client = r["source_id"].split(":")[0]
        api = by_client.get(r["source_id"])
        if api is None:
            api = by_client[r["source_id"]] = Legistar(fetcher, _client_of(r["url"]), (tokens or {}).get(client))
            if "/matters/" in r["url"]:
                api.base = r["url"].split("/matters/")[0]   # the host the record was read from
        try:
            sp = api.sponsors(m["MatterId"])
        except Exception as exc:  # noqa: BLE001
            log(f"  sponsors {r['source_id']} {m.get('MatterFile')}: {type(exc).__name__}: {exc}"[:200])
            continue
        checked += 1
        m["sponsors"] = sp
        m["sponsors_modified"] = m.get("MatterLastModifiedUtc") or ""
        m["opened_url"] = f"{api.base}/matters/{m['MatterId']}/sponsors"
        store.add(Record(r["source_id"], r["kind"], r["url"], title=r.get("title") or "", text=r.get("text") or "",
                         markdown=r.get("markdown") or "", published=r.get("published"), channel="api",
                         target=r.get("target") or "", evidence=r.get("evidence") or "primary", meta=m,
                         key=r.get("key") or None))
        if sp:
            found += 1
    store.flush()
    return {"checked": checked, "with_sponsors": found}


def _client_of(url: str) -> str:
    """'https://webapi.legistar.com/v1/canton/matters/5' -> 'canton'."""
    parts = url.split("/v1/", 1)
    return parts[1].split("/")[0] if len(parts) == 2 else ""


# ----------------------------------------------------------------------------- adapter interface
NAME = "legistar"


def detect(src: dict, home_html: str, hints: dict) -> str | None:
    from urllib.parse import urlsplit

    client = src.get("legistar") or hints.get("legistar_client")
    if not client and urlsplit(src["url"]).netloc.endswith(".legistar.com"):
        client = urlsplit(src["url"]).netloc.split(".")[0]
    return client or None


def probe(fetcher: Fetcher, src: dict, client: str) -> int:
    return Legistar(fetcher, client, src.get("legistar_token")).probe()


def crawl(fetcher: Fetcher, src: dict, client: str, since: str, modified_since: str | None, ctx, limits: dict):
    lg = Legistar(fetcher, client, src.get("legistar_token"))
    lim = src.get("max_items")
    yield from lg.matters(since, src.get("matter_types"), lim, modified_since=modified_since, cursors=ctx.cursors)
    try:
        yield from lg.officials()            # office records: who sits on which body, when (also the vote sweep's people)
    except RuntimeError:
        pass                                 # some deployments don't expose office records
    yield from lg.meetings(since, src.get("votes", True), lim, modified_since=modified_since, cursors=ctx.cursors,
                           lookback_days=src.get("meeting_lookback_days", limits.get("meeting_lookback_days", 45)),
                           store=ctx.frontier, source_id=src["id"], votes_mode=src.get("votes_mode", "person"))


def shard_key(src: dict) -> str | None:
    return "webapi.legistar.com" if src.get("legistar") else None


import sys as _sys  # noqa: E402

from . import register as _register  # noqa: E402

_register(_sys.modules[__name__])
