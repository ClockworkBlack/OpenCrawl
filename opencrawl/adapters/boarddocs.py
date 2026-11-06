"""BoardDocs adapter (go.boarddocs.com) — school boards and some county boards.

A BoardDocs public site is one page (Board.nsf/Public) whose JavaScript loads
everything else from read-only endpoints under the same Board.nsf:
  BD-GetMeetingsList?open   (POST current_committee_id)   -> JSON list of meetings
  BD-GetAgenda?open         (POST current_committee_id,id) -> the meeting's agenda as HTML
  goto?open&id=<item>       (GET)                           -> one agenda item's public page
  files/<id>/$file/<name>   (GET)                           -> an attachment (PDF)
This adapter calls exactly those, the way the page itself does, and cites the
goto?open&id= permalink BoardDocs prints for each item (a link the site gave)
or, for a meeting, the agenda resource that was read.

Records
  meeting      one per meeting (board, date, name)
  agenda_item  one per item: category, title, type, and the item's own text when its
               page was opened (opened: true) — recent meetings and action items first
  document     attachments that were opened (minutes, resolutions, policies): their text
               flows into the minutes deriver, so board votes come out of BoardDocs minutes too
Budget: `max_items` agenda pages and `max_docs` attachments per run; everything else is
listed from the agenda (title + permalink, opened: false) and picked up on a later run.
Resumable: meetings already stored are skipped unless they fall inside the lookback window.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
from typing import Iterator
from urllib.parse import urljoin

from .. import extract
from ..fetch import Blocked, Disallowed, Fetcher
from ..store import Record

BASE_RX = re.compile(r"(https?://[^/]+/[a-z]{2}/[a-z0-9\-]+/Board\.nsf)", re.I)
ID_RX = r"[A-Z0-9]{6,}"
ACTION_RX = re.compile(r"\b(action|vote|approv|adopt|resolution|policy|budget|millage|contract|superintendent|personnel|"
                       r"redistrict|attendance zone|calendar|charter|bond|levy|curriculum|textbook|book|library|"
                       r"discipline|safety|closure|consolidat|salary|tax)\b", re.I)
DOC_RX = re.compile(r"\b(minutes|resolution|policy|budget|agenda)\b", re.I)


def base_of(url: str) -> str | None:
    m = BASE_RX.search(url or "")
    return m.group(1) if m else None


class BoardDocs:
    def __init__(self, fetcher: Fetcher, public_url: str):
        self.f = fetcher
        self.base = base_of(public_url) or public_url.rstrip("/").removesuffix("/Public")
        self.public = self.base + "/Public"

    # ------------------------------------------------------------------ calls
    def _post(self, name: str, data: dict) -> str:
        r = self.f.post(f"{self.base}/{name}?open&{random.random()}", data=data,
                        headers={"Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                                 "X-Requested-With": "XMLHttpRequest"})
        if r.status != 200:
            raise RuntimeError(f"BoardDocs {name}: HTTP {r.status}")
        return r.text

    def committees(self) -> list[dict]:
        r = self.f.get(self.public)
        if r.status != 200:
            raise RuntimeError(f"BoardDocs Public: HTTP {r.status}")
        html = r.text
        out, seen = [], set()
        soup = extract.soup_of(html)
        # 1. elements marked as committees (class or data attribute), 2. a select of committees, 3. ids in the script
        for el in soup.select('[class*="committee"], [data-committee], [data-committee-id], select#committees option, '
                              'select[name*="committee"] option'):
            cid = el.get("data-committee-id") or el.get("data-committee") or el.get("id") or el.get("value") or ""
            cid = cid.strip()
            name = el.get_text(" ", strip=True)[:120]
            if re.fullmatch(ID_RX, cid) and cid not in seen:
                seen.add(cid)
                out.append({"id": cid, "name": name})
        for m in re.finditer(r"current_committee_id['\"]?\s*[:=]\s*['\"](" + ID_RX + r")['\"]", html):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append({"id": m.group(1), "name": ""})
        return out

    def meetings(self, committee_id: str) -> list[dict]:
        text = self._post("BD-GetMeetingsList", {"current_committee_id": committee_id})
        rows: list = []
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else data.get("meetings") or data.get("data") or []
        except ValueError:
            for m in re.finditer(r'id="(' + ID_RX + r')"[^>]*>(.*?)</a>', text, re.S):
                rows.append({"unique": m.group(1), "name": re.sub(r"<[^>]+>", " ", m.group(2)).strip()})
        out = []
        for r in rows:
            uid = r.get("unique") or r.get("id") or r.get("key")
            if not uid:
                continue
            nd = str(r.get("numberdate") or r.get("date") or "")
            date = f"{nd[:4]}-{nd[4:6]}-{nd[6:8]}" if re.fullmatch(r"\d{8}", nd) else (nd[:10] if re.match(r"\d{4}-\d\d-\d\d", nd) else "")
            out.append({"id": uid, "name": (r.get("name") or "").strip(), "date": date, "committee": committee_id,
                        "current": bool(r.get("current"))})
        out.sort(key=lambda m: m["date"], reverse=True)
        return out

    def agenda(self, committee_id: str, meeting_id: str) -> list[dict]:
        html = self._post("BD-GetAgenda", {"current_committee_id": committee_id, "id": meeting_id})
        soup = extract.soup_of(html)
        items, category, order = [], "", 0
        for el in soup.find_all(True):
            cls = " ".join(el.get("class") or [])
            if el.name in ("h2", "h3", "h4") or "category" in cls:
                t = el.get_text(" ", strip=True)
                if t and not re.fullmatch(ID_RX, t):
                    category = t[:160]
                continue
            if el.name == "a" and re.fullmatch(ID_RX, el.get("id") or "") and el.get_text(strip=True):
                order += 1
                items.append({"id": el["id"], "title": el.get_text(" ", strip=True)[:300], "category": category, "order": order,
                              "url": f"{self.base}/goto?open&id={el['id']}"})
        return items

    def item(self, item_id: str) -> dict | None:
        r = self.f.get(f"{self.base}/goto?open&id={item_id}")
        if r.status != 200 or "html" not in (r.content_type or ""):
            return None
        soup = extract.soup_of(r.text)
        title = ""
        for sel in ("h1", "h2", ".title", "#title", "title"):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                title = el.get_text(" ", strip=True)[:300]
                break
        _, md = extract.html_to_markdown(r.text, r.url)
        kind = ""
        m = re.search(r"\b(Action|Information|Discussion|Consent|Recognition|Procedural|Report)\b(?:\s+\(Consent\))?", r.text)
        if m:
            kind = m.group(0)
        files = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(r"/files/[A-Z0-9]+/\$file/", h, re.I) or re.search(r"\$file/", h, re.I):
                files.append({"url": urljoin(r.url, h), "name": a.get_text(" ", strip=True)[:200] or h.rsplit("/", 1)[-1]})
        return {"url": r.url, "title": title, "text": md, "type": kind, "files": files}

    def probe(self) -> int:
        cs = self.committees()
        if not cs:
            return 0
        return len(self.meetings(cs[0]["id"]))

    # ------------------------------------------------------------------ records
    def records(self, source_id: str, target: str, since: str = "2016-01-01", modified_since: str | None = None,
                store=None, max_items: int = 120, max_docs: int = 40, lookback_days: int = 45,
                src: dict | None = None) -> Iterator[Record]:
        opened_items = opened_docs = 0
        recent = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
        for com in self.committees():
            try:
                meetings = self.meetings(com["id"])
            except (Blocked, Disallowed):
                raise
            except Exception:  # noqa: BLE001
                continue
            for mt in meetings:
                if mt["date"] and mt["date"] < since:
                    continue
                mkey = f"{source_id}|meeting|boarddocs|{mt['id']}"
                if store is not None and store.has(mkey) and (modified_since or True) and (not mt["date"] or mt["date"] < recent):
                    continue  # already stored and outside the lookback window
                try:
                    items = self.agenda(com["id"], mt["id"])
                except (Blocked, Disallowed):
                    raise
                except Exception:  # noqa: BLE001
                    items = []
                agenda_res = f"{self.base}/BD-GetAgenda?open&id={mt['id']}"
                yield Record(source_id, "meeting", f"{self.base}/goto?open&id={mt['id']}", title=f"{com['name'] or 'Board'} {mt['name']} {mt['date']}".strip(),
                             text="\n".join(f"{i['order']}. {i['title']}" for i in items)[:20000], published=mt["date"] or None,
                             channel="api", target=target, key=mkey,
                             meta={"provider": "boarddocs", "meeting_id": mt["id"], "committee": com["id"], "committee_name": com["name"],
                                   "EventBodyName": com["name"] or "Board of Education", "items": len(items), "opened": True,
                                   "opened_url": agenda_res})
                # action items and recent meetings get their pages opened; the rest are listed from the agenda
                ranked = sorted(items, key=lambda i: (not ACTION_RX.search(f"{i['category']} {i['title']}"), i["order"]))
                for it in ranked:
                    detail = None
                    if opened_items < max_items and (ACTION_RX.search(f"{it['category']} {it['title']}") or (mt["date"] or "") >= recent):
                        try:
                            detail = self.item(it["id"])
                            opened_items += 1
                        except (Blocked, Disallowed):
                            raise
                        except Exception:  # noqa: BLE001
                            detail = None
                    text = (detail or {}).get("text") or it["title"]
                    yield Record(source_id, "agenda_item", it["url"], title=(detail or {}).get("title") or it["title"], text=text[:30000],
                                 published=mt["date"] or None, channel="api" if detail else "listed", target=target,
                                 key=f"{source_id}|agenda_item|boarddocs|{it['id']}",
                                 meta={"provider": "boarddocs", "item_id": it["id"], "meeting_id": mt["id"], "category": it["category"],
                                       "order": it["order"], "type": (detail or {}).get("type", ""), "EventBodyName": com["name"] or "Board of Education",
                                       "opened": bool(detail), "attachments": [f["name"] for f in (detail or {}).get("files", [])][:20]})
                    for fdoc in (detail or {}).get("files", []):
                        if opened_docs >= max_docs or not DOC_RX.search(f"{fdoc['name']} {it['title']}"):
                            continue
                        if store is not None and store.has(f"{source_id}|document|boarddocs|{fdoc['url']}"):
                            continue
                        try:
                            r = self.f.get(fdoc["url"])
                        except (Blocked, Disallowed):
                            raise
                        except Exception:  # noqa: BLE001
                            continue
                        if r.status != 200:
                            continue
                        from ..channels import _doc_record

                        rec = _doc_record(r, source_id, src or {}, title=fdoc["name"])
                        if rec:
                            opened_docs += 1
                            rec.target = target
                            rec.published = rec.published or mt["date"] or None
                            rec.key = f"{source_id}|document|boarddocs|{fdoc['url']}"
                            rec.meta.update({"provider": "boarddocs", "meeting_id": mt["id"], "item_id": it["id"],
                                             "EventBodyName": com["name"] or "Board of Education"})
                            if re.search(r"minutes", fdoc["name"], re.I):
                                rec.meta["doc_type"] = "minutes"
                            yield rec


# ----------------------------------------------------------------------------- adapter interface
NAME = "boarddocs"


def detect(src: dict, home_html: str, hints: dict) -> str | None:
    return src.get("boarddocs") or (src["url"] if "go.boarddocs.com" in src["url"] else None)


def probe(fetcher: Fetcher, src: dict, client: str) -> int:
    return BoardDocs(fetcher, client).probe()


def crawl(fetcher: Fetcher, src: dict, client: str, since: str, modified_since: str | None, ctx, limits: dict):
    yield from BoardDocs(fetcher, client).records(
        src["id"], src.get("target", ""), since, modified_since=modified_since, store=ctx.frontier if ctx else None,
        max_items=int(src.get("max_items") or 120), max_docs=int(src.get("max_docs") or 40),
        lookback_days=src.get("meeting_lookback_days", limits.get("meeting_lookback_days", 45)), src=src)


def shard_key(src: dict) -> str | None:
    return None


import sys as _sys  # noqa: E402

from . import register as _register  # noqa: E402

_register(_sys.modules[__name__])
