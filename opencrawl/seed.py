"""`opencrawl seed`: find where each target's records live.

For every target in the registry it fills state/discovered.json with:

  website        the target's site. Given in targets.yaml / overrides.yaml, else looked up: Wikidata (by the
                 target's `wikidata` QID, or the official-website claim of its Wikipedia article), then the
                 Wikipedia infobox of `wikipedia` (a URL), then a Wikipedia search by name.
  portals        structured systems and official social links found on the site (opencrawl.targets.PORTAL_PATTERNS:
                 YouTube, Facebook, and whatever the civic module and adapters register — meeting portals, code
                 libraries...)
  feeds          RSS/Atom feeds the site advertises
  hints          API hints (WordPress REST, Legistar client)
  records        a public-records / clerk contact printed on the site (email on an official domain, or a portal)

Every request goes through the normal polite fetcher (robots.txt, spacing, honest User-Agent). Wikimedia asks
exactly that of automated clients.

Extension points
  FOLLOW_LINKS   regex of link texts / paths worth opening from the home page to find portals and feeds
  INSPECTORS     functions(fetcher, home_html, home_url, out) called after the home page is read
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from . import extract
from .blocked import Blocked
from .fetch import Disallowed, Fetcher, OutOfTime
from .targets import find_portals

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
WIKI_API = "https://en.wikipedia.org/w/api.php"
NON_OFFICIAL = ("cloudinary.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "linkedin.com",
                "wikipedia.org", "wikidata.org", "google.com", "mailto:", "tel:")

# pages linked from the home page that usually hold the portals and feeds a home page does not link directly
FOLLOW_LINKS = re.compile(r"\b(agendas?|minutes|meetings?|council|commission|board|news|press|media|newsroom|blog|"
                          r"publications?|reports?|documents?|library|about)\b", re.I)
FOLLOW_PAGES = 4
INSPECTORS: list[Callable[[Fetcher, str, str, dict], None]] = []


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _clean_site(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    s = urlsplit(url)
    if not s.netloc or any(b in url.lower() for b in NON_OFFICIAL):
        return None
    return f"{s.scheme}://{s.netloc}{s.path or '/'}"


# ----------------------------------------------------------------------------- wikidata / wikipedia
def wikidata_website(fetcher: Fetcher, qid: str) -> str | None:
    """The official website (P856) of one Wikidata item."""
    r = fetcher.get(WIKIDATA_ENTITY.format(qid=qid), use_cache=False)
    if r.status != 200:
        return None
    ent = (r.json().get("entities") or {}).get(qid) or {}
    for claim in (ent.get("claims") or {}).get("P856") or []:
        v = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, str):
            return _clean_site(v)
    return None


def wiki_title(wiki_url: str) -> str:
    """"https://en.wikipedia.org/wiki/Aurora,_Colorado" -> "Aurora, Colorado"."""
    from urllib.parse import unquote

    return unquote(wiki_url.rsplit("/wiki/", 1)[-1]).replace("_", " ").split("#")[0]


def wikidata_websites_by_titles(fetcher: Fetcher, titles: list[str], batch: int = 60) -> dict[str, str]:
    """{English Wikipedia article title: official website (P856)}, one SPARQL query per `batch` titles. Titles are
    matched against Wikidata's sitelinks, so a redirect title ("Denver, Colorado" for "Denver") does not match;
    the infobox lookup covers those."""
    out: dict[str, str] = {}
    titles = [x for x in dict.fromkeys(titles) if x]
    for i in range(0, len(titles), batch):
        chunk = titles[i:i + batch]
        values = " ".join('"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"@en' for x in chunk)
        query = f"""SELECT ?title ?web WHERE {{
          VALUES ?title {{ {values} }}
          ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title .
          ?item wdt:P856 ?web .
        }}"""
        r = fetcher.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                        headers={"Accept": "application/sparql-results+json"}, use_cache=False)
        if r.status != 200:
            raise RuntimeError(f"wikidata HTTP {r.status}")
        for row in r.json()["results"]["bindings"]:
            out.setdefault(row["title"]["value"], row["web"]["value"])
    return out


def wikipedia_website(fetcher: Fetcher, wiki_url: str) -> str | None:
    """The `website =` field of a Wikipedia article's infobox."""
    title = wiki_url.rsplit("/wiki/", 1)[-1]
    r = fetcher.get(WIKI_API, params={"action": "parse", "page": title, "prop": "wikitext", "format": "json",
                                      "redirects": 1}, use_cache=False)
    if r.status != 200:
        return None
    wt = r.json().get("parse", {}).get("wikitext", {}).get("*", "")
    m = re.search(r"\|\s*(?:website|homepage|url)\s*=\s*(.+)", wt, re.I)
    if not m:
        return None
    val = m.group(1)
    m2 = re.search(r"\{\{\s*URL\s*\|\s*([^|}]+)", val, re.I) or re.search(r"\[?(https?://[^\s\]|}]+)", val) \
        or re.search(r"([a-z0-9.\-]+\.(?:gov|org|com|us|net|edu|io|co)[^\s|}\]]*)", val, re.I)
    return _clean_site(m2.group(1)) if m2 else None


def _alnum_s(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def wikipedia_search_website(fetcher: Fetcher, names: list[str], hint: str = "") -> tuple[str | None, str | None]:
    """Search Wikipedia for an article by the target's names (plus a disambiguating hint such as a state or
    industry); (website, article url) from the first article whose title names the target."""
    for name in names[:3]:
        try:
            r = fetcher.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": f"{name} {hint}".strip(),
                                              "srlimit": 3, "format": "json"}, use_cache=False)
        except Exception:  # noqa: BLE001
            return None, None
        if r.status != 200:
            continue
        for hit in r.json().get("query", {}).get("search", []):
            title = hit.get("title", "")
            if _alnum_s(name) in _alnum_s(title) or _alnum_s(title) in _alnum_s(name):
                url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
                try:
                    web = wikipedia_website(fetcher, url)
                except Exception:  # noqa: BLE001
                    web = None
                if web:
                    return web, url
    return None, None


# ----------------------------------------------------------------------------- website inspection
def inspect_site(fetcher: Fetcher, website: str) -> dict:
    out: dict = {}
    try:
        r = fetcher.get(website)
    except (Disallowed, Blocked) as exc:
        return {"site_error": f"{type(exc).__name__}: {exc}"[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"site_error": f"{type(exc).__name__}: {exc}"[:200]}
    if r.status != 200 or "html" not in r.content_type:
        return {"site_error": f"HTTP {r.status} {r.content_type}"}
    html = r.text
    out["website"] = _clean_site(r.url) or website  # follow redirects to the canonical host
    out["portals"] = find_portals(html, r.url)
    out["feeds"] = extract.find_feed_links(html, r.url)[:5]
    out["hints"] = extract.find_api_hints(html, r.url)
    # CivicPlus sites: the AgendaCenter usually isn't linked from the home page by that name
    if "agenda_center" not in out["portals"] and ("civicplus" in html.lower() or "/AgendaCenter" in html):
        probe = urljoin(r.url, "/AgendaCenter")
        try:
            pr = fetcher.get(probe)
            if pr.status == 200 and "AgendaCenter" in pr.text:
                out["portals"]["agenda_center"] = probe
                out["feeds"] += [f for f in extract.find_feed_links(pr.text, pr.url) if f not in out["feeds"]][:5]
        except (Disallowed, Blocked):
            pass
    # portals and feeds are usually linked from a section page ("Agendas & Minutes", "Newsroom") rather than home
    known = {v.rstrip("/").lower() for v in out["portals"].values()}
    pages = [u for u in follow_pages(html, r.url) if u.rstrip("/").lower() not in known][:FOLLOW_PAGES]
    structured = {"legistar", "granicus", "civicclerk", "boarddocs", "iqm2", "escribe", "novusagenda"}
    for i, page_url in enumerate(pages):
        if i >= 2 and structured & set(out["portals"]):
            break  # the first two section pages are always read; more only while no structured portal is known
        try:
            pr = fetcher.get(page_url)
        except (Disallowed, Blocked):
            break
        except Exception:  # noqa: BLE001
            continue
        if pr.status == 200 and "html" in pr.content_type:
            for k, v in find_portals(pr.text, pr.url).items():
                out["portals"].setdefault(k, v)
            out["feeds"] += [f for f in extract.find_feed_links(pr.text, pr.url) if f not in out["feeds"]][:3]
            if "section_page" not in out:
                out["section_page"] = pr.url
    for fn in INSPECTORS:
        try:
            fn(fetcher, html, r.url, out)
        except (Disallowed, Blocked):
            pass
        except Exception:  # noqa: BLE001 — an inspector must not break seeding
            pass
    try:
        rc = find_records_contact(fetcher, html, r.url)
        if rc:
            out["records"] = rc
    except (Disallowed, Blocked):
        pass
    return out


def follow_pages(home_html: str, home_url: str) -> list[str]:
    """Internal links from the home page worth opening to find portals and feeds, best first."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    scored = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(home_url, a["href"])
        if urlsplit(href).netloc.lower().removeprefix("www.") != host or href.split("#")[0] == home_url.split("#")[0]:
            continue
        hay = f"{text} {urlsplit(href).path}"
        if not FOLLOW_LINKS.search(hay):
            continue
        score = 3 if re.search(r"agenda|minute|newsroom|press", hay, re.I) else 2 if re.search(r"council|commission|board|news", hay, re.I) else 1
        scored.append((-score, len(href), href.split("#")[0]))
    seen, out = set(), []
    for _, _, href in sorted(scored):
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out




RECORDS_LINK = re.compile(r"open[\s-]*records|public[\s-]*records|records[\s-]*request|right[\s-]*to[\s-]*know|"
                          r"\bfoia\b|\bora\b|city[\s-]*clerk|county[\s-]*clerk|clerk[\s-]*of[\s-]*(council|the|commission)|"
                          r"clerk['’]?s[\s-]*office", re.I)
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RECORDS_PORTALS = re.compile(r"https?://[^\s\"'<>]*(govqa\.us|nextrequest\.com|justfoia\.com|mycusthelp\.com|"
                             r"civicplus\.help|openrecords\.)[^\s\"'<>]*", re.I)
OFFICIAL_EMAIL_SUFFIXES: tuple[str, ...] = (".gov", ".us", ".mil", ".edu")


def _official_email(addr: str, site: str) -> bool:
    """An address on the target's own domain, or on a government / institutional domain."""
    dom = addr.rsplit("@", 1)[-1].lower()
    host = urlsplit(site).netloc.lower().removeprefix("www.")
    base = ".".join(host.split(".")[-2:])
    return dom == host or dom.endswith("." + base) or dom == base or dom.endswith(OFFICIAL_EMAIL_SUFFIXES)


def find_records_contact(fetcher: Fetcher, home_html: str, home_url: str) -> dict | None:
    """Follow the site's own public-records / clerk link and take the request email or portal it publishes.
    Only addresses printed on the site itself (and on an official domain) are kept; nothing is guessed."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    cands = []
    for a in soup.find_all("a", href=True):
        label = f"{a.get_text(' ', strip=True)} {a['href']}"
        if RECORDS_LINK.search(label):
            u = urljoin(home_url, a["href"]).split("#")[0]
            if urlsplit(u).netloc.lower().removeprefix("www.") == host or RECORDS_PORTALS.search(u):
                score = 2 if re.search(r"records", label, re.I) else 1
                cands.append((score, u))
    for _, u in sorted(dict.fromkeys(cands), key=lambda x: -x[0])[:2]:
        if RECORDS_PORTALS.search(u):
            return {"portal": u, "page": home_url}
        r = fetcher.get(u)
        if r.status != 200 or "html" not in r.content_type:
            continue
        page = r.text
        mails = [m.group(0).rstrip(".") for m in EMAIL_RX.finditer(page)]
        mails += [a["href"][7:].split("?")[0] for a in extract.soup_of(page).find_all("a", href=True)
                  if a["href"].lower().startswith("mailto:")]
        mails = [m for m in dict.fromkeys(x.strip().lower() for x in mails) if _official_email(m, home_url)]
        records_first = sorted(mails, key=lambda m: (not re.search(r"record|foia|ora|clerk", m), len(m)))
        portal = RECORDS_PORTALS.search(page)
        if records_first or portal:
            return {"emails": records_first[:3], "portal": portal.group(0) if portal else None, "page": r.url}
    return None


# ----------------------------------------------------------------------------- driver
def _stale(rec: dict | None, max_age_days: float | None) -> bool:
    if not rec:
        return True
    if max_age_days is None:
        return False
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(rec.get("checked_at", ""))
    except ValueError:
        return True
    return age.total_seconds() > max_age_days * 86400


class Discovered:
    """state/discovered.json: one entry per target, saved every few targets and at the end."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.lock = threading.Lock()

    def put(self, tid: str, rec: dict):
        with self.lock:
            self.data[tid] = rec

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
            tmp.replace(self.path)


def find_website(fetcher: Fetcher, t: dict, log=print) -> tuple[str | None, str, dict]:
    """(website, how it was found, extra fields) for a generic target."""
    extra: dict = {}
    web = _clean_site(t.get("website"))
    if web:
        return web, "configured", extra
    if t.get("wikidata"):
        try:
            web = wikidata_website(fetcher, t["wikidata"])
            if web:
                return web, "wikidata", extra
        except Exception as exc:  # noqa: BLE001
            extra["wikidata_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if t.get("_title_website"):
        return _clean_site(t["_title_website"]), "wikidata", extra
    if t.get("wikipedia"):
        try:
            web = wikipedia_website(fetcher, t["wikipedia"])
            if web:
                return web, "wikipedia", extra
        except Exception as exc:  # noqa: BLE001
            extra["wikipedia_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if t.get("lookup", True):
        web, page = wikipedia_search_website(fetcher, [t["name"]] + list(t.get("aliases") or []), t.get("search_hint", ""))
        if web:
            extra["wikipedia"] = page
            return web, "wikipedia search", extra
    return None, "", extra


def seed(fetcher: Fetcher, targets: list[dict], state_path: Path, workers: int = 6, only: str | None = None,
         refresh: bool = False, log=print, max_age_days: float | None = None, deadline: float | None = None,
         find=find_website) -> dict:
    """Discover websites/portals/feeds for `targets`. Re-checks entries older than max_age_days (all with refresh);
    stops starting new targets once `deadline` (time.monotonic) passes; resumable. `find` looks up a website
    (the civic module passes its FIPS-based lookup)."""
    import time as _time

    disc = Discovered(state_path)
    todo = [t for t in targets if t.get("enabled") is not False and (not only or t["id"] == only)
            and (refresh or _stale(disc.data.get(t["id"]), max_age_days))]
    # biggest first: if time runs out, the targets that matter most are done
    todo.sort(key=lambda t: (-(t.get("population") or 0), {"a": 0, "b": 1, "c": 2}.get(t.get("tier", "b"), 1)))
    log(f"seeding {len(todo)} of {len(targets)} targets")
    # one batched Wikidata lookup for every target that names its Wikipedia article (a few queries, not one each)
    by_title: dict[str, str] = {}
    titles = [wiki_title(t["wikipedia"]) for t in todo if t.get("wikipedia") and not t.get("website")]
    if titles:
        try:
            by_title = wikidata_websites_by_titles(fetcher, titles)
            log(f"wikidata: {len(by_title)} websites for {len(titles)} articles")
        except Exception as exc:  # noqa: BLE001
            log(f"wikidata unavailable ({exc}); falling back to Wikipedia")

    def one(t: dict):
        if deadline is not None and _time.monotonic() > deadline:
            return t["id"], None
        rec: dict = {"checked_at": _now(), "portals": {}, "feeds": []}
        if t.get("wikipedia") and by_title.get(wiki_title(t["wikipedia"])):
            t = {**t, "_title_website": by_title[wiki_title(t["wikipedia"])]}
        try:
            website, how, extra = find(fetcher, t, log)
        except OutOfTime:
            return t["id"], None
        except Exception as exc:  # noqa: BLE001
            website, how, extra = None, "", {"lookup_error": f"{type(exc).__name__}: {exc}"[:200]}
        rec.update(extra)
        if website:
            rec["website"], rec["website_source"] = website, how
            if deadline is not None and _time.monotonic() > deadline:
                return t["id"], None          # budget spent during the lookup: inspect next run
            rec.update(inspect_site(fetcher, website))
            if (rec.get("site_error") or "").startswith("OutOfTime"):
                return t["id"], None
        disc.put(t["id"], rec)
        return t["id"], rec

    done = 0
    fetcher.stop_at = deadline            # a lookup mid-way stops at its next request once the budget is spent
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for tid, rec in pool.map(one, todo):
                if rec is None:
                    continue  # time budget used up; picked up next run
                done += 1
                log(f"[{done}/{len(todo)}] {tid}: {rec.get('website', '-')} portals={','.join(rec.get('portals', {}).keys()) or '-'}")
                if done % 25 == 0:
                    disc.save()
    finally:
        fetcher.stop_at = None
        disc.save()
    return disc.data
