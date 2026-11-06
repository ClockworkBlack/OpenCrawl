"""Jurisdiction registry (civic module): every county, municipality and school district of a state, as targets.

Layers, lowest to highest priority:
  config/jurisdictions.yaml    generated registry (name, kind, FIPS, counties, population, county seat...)
  config/school_districts.yaml public school districts (kind school_district)
  state/discovered.json        machine discoveries from `opencrawl seed` (website, portals, feeds, records contact)
  config/overrides.yaml        hand edits under `targets:` (website, portals, enabled, tier, notes)

Neither file is required: without them this module contributes nothing and `targets.yaml` is the whole registry.
The example project under opencrawl/examples/colorado-civic holds a complete registry for one US state, built by
opencrawl/examples/colorado-civic/scripts/build_registry.py from public Wikipedia / Census lists; the same script works for
any state.

A jurisdiction expands into one crawl source per place its records live (opencrawl.targets.sources_for):
  <id>:legistar   Legistar API            (council records, roll-call votes)
  <id>:civicclerk CivicClerk              (meetings, agendas, minutes)
  <id>:boarddocs  BoardDocs               (school boards: meetings, agenda items, minutes)
  <id>:agendas    CivicPlus AgendaCenter  (agendas/minutes; RSS)
  <id>:granicus   Granicus                (agendas/minutes; RSS)
  <id>:elections  elections office site   (results, qualifying lists, sample ballots)
  <id>:code       eCode360 / amlegal      (ordinances; browser channel)
  <id>:youtube    official YouTube channel (meeting videos; needs YOUTUBE_API_KEY)
  <id>:site       official website        (pages, PDFs, feeds; auto channel)
Municode links are recorded but not crawled (its library blocks crawlers; see opencrawl.blocked).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..targets import (PORTAL_PATTERNS, PORTAL_SOURCES, SITE_SOURCE_HOOKS, SOURCE_PROVIDERS, TARGET_PROVIDERS,
                       TIER_CADENCE_DAYS, by_id as _by_id, find_portals, load_json, load_yaml, targets as _targets)

__all__ = ["jurisdictions", "by_id", "sources_for", "all_sources", "find_portals", "load_yaml", "load_json",
           "tier_of", "school_districts", "press_sources", "guide_sources", "TIER_CADENCE_DAYS", "PORTAL_PATTERNS"]

PORTAL_PATTERNS.update({
    "legistar": re.compile(r"https?://([a-z0-9\-]+)\.legistar\.com", re.I),
    "granicus": re.compile(r"https?://([a-z0-9\-]+)\.granicus\.com[^\s\"'<>]*", re.I),
    "civicclerk": re.compile(r"https?://([a-z0-9\-]+)\.portal\.civicclerk\.com", re.I),
    "boarddocs": re.compile(r"https?://go\.boarddocs\.com/[^\s\"'<>]+", re.I),
    "agenda_center": re.compile(r"https?://[^\s\"'<>]+/AgendaCenter[^\s\"'<>]*", re.I),
    "municode": re.compile(r"https?://library\.municode\.com/[a-z]{2}/[a-z0-9_\-]+", re.I),
    "amlegal": re.compile(r"https?://codelibrary\.amlegal\.com/codes/[a-z0-9_\-]+", re.I),
    "ecode360": re.compile(r"https?://(?:www\.)?ecode360\.com/[A-Z]{2}\d{3,5}", re.I),
    "iqm2": re.compile(r"https?://[a-z0-9\-]+\.iqm2\.com[^\s\"'<>]*", re.I),
    "escribe": re.compile(r"https?://pub-[a-z0-9\-]+\.escribemeetings\.com[^\s\"'<>]*", re.I),
    "novusagenda": re.compile(r"https?://[a-z0-9\-]+\.novusagenda\.com[^\s\"'<>]*", re.I),
    "diligent": re.compile(r"https?://[a-z0-9\-]+\.diligent\.com/[^\s\"'<>]*community[^\s\"'<>]*", re.I),
})

CIVIC_KINDS = {"county", "city", "town", "consolidated", "school_district", "village", "borough", "township"}


def tier_of(j: dict) -> str:
    """Crawl cadence tier from population: a = counties, consolidated governments and cities >= 10k; b = 1k-10k;
    c = under 1k. School boards meet monthly: b for the big systems, c otherwise."""
    if j.get("tier"):
        return j["tier"]
    pop = j.get("population") or 0
    if j.get("kind") == "school_district":
        return "b" if pop >= 20000 else "c"
    if j.get("kind") in ("county", "consolidated") or pop >= 10000:
        return "a"
    return "b" if pop >= 1000 else "c"


def school_districts(root: Path) -> list[dict]:
    return list(load_yaml(Path(root) / "config" / "school_districts.yaml").get("districts") or [])


def registry_entries(root: Path) -> list[dict]:
    """Raw registry rows (before discovery and overrides), each with its population tier."""
    reg = load_yaml(Path(root) / "config" / "jurisdictions.yaml")
    out = []
    for j in (reg.get("counties", []) + reg.get("municipalities", []) + school_districts(root)):
        j = dict(j)
        j.setdefault("kind", "city")
        j["tier"] = tier_of(j)
        out.append(j)
    return out


TARGET_PROVIDERS.append(registry_entries)


def jurisdictions(root: Path) -> list[dict]:
    """The registry's targets after discovery and overrides (every target whose kind is a civic kind)."""
    return [t for t in _targets(root) if t.get("kind") in CIVIC_KINDS or t.get("fips")]


def by_id(root: Path) -> dict[str, dict]:
    return _by_id(root)


# ----------------------------------------------------------------------------- portal expansion
def _legistar(j: dict, base: dict, url: str) -> dict:
    client = PORTAL_PATTERNS["legistar"].search(url)
    client = client.group(1).lower() if client else url
    return {**base, "id": f"{j['id']}:legistar", "url": f"https://{client}.legistar.com/", "legistar": client,
            "kind": "council", "channel": "api", "endpoint": f"legistar:{client}"}


def _civicclerk(j: dict, base: dict, url: str) -> dict:
    cc = PORTAL_PATTERNS["civicclerk"].search(url)
    client = cc.group(1).lower() if cc else url
    return {**base, "id": f"{j['id']}:civicclerk", "url": f"https://{client}.portal.civicclerk.com",
            "civicclerk": client, "kind": "meeting", "channel": "api", "endpoint": f"civicclerk:{client}"}


def _boarddocs(j: dict, base: dict, url: str) -> dict:
    return {**base, "id": f"{j['id']}:boarddocs", "url": url, "kind": "meeting", "channel": "api",
            "endpoint": f"boarddocs:{url}", "max_items": 120, "max_docs": 40}


def _elections(j: dict, base: dict, url: str) -> dict:
    return {**base, "id": f"{j['id']}:elections", "url": url, "kind": "election", "depth": 3, "max_pages": 150,
            "coverage": True,
            "include": ["result", "qualif", "candidate", "runoff", "ballot", "election", "returns", "canvass",
                        "referendum", "ballot", "precinct", "summary"],
            "exclude": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/"]}


def _agenda_center(j: dict, base: dict, url: str) -> dict:
    return {**base, "id": f"{j['id']}:agendas", "url": url, "kind": "meeting", "follow_feed_links": True,
            "include": ["/AgendaCenter/", "/DocumentCenter/", "/Archive"], "max_pages": 150}


def _granicus_rss(url: str) -> str | None:
    m = re.search(r"(https?://[a-z0-9\-]+\.granicus\.com)/ViewPublisher\.php\?view_id=(\d+)", url, re.I)
    if m:
        return f"{m.group(1)}/ViewPublisherRSS.php?view_id={m.group(2)}&mode=agendas"
    return None


def _granicus(j: dict, base: dict, url: str) -> dict:
    return {**base, "id": f"{j['id']}:granicus", "url": url, "kind": "meeting", "max_pages": 150,
            "feeds": [_granicus_rss(url)] if _granicus_rss(url) else None}


def _code(follow: str):
    def make(j: dict, base: dict, url: str) -> dict:
        # both code libraries render section bodies client-side: headless-browser channel, follow the TOC links
        return {**base, "id": f"{j['id']}:code", "url": url, "kind": "ordinance", "max_pages": 400,
                "render": "browser", "follow": follow}
    return make


PORTAL_SOURCES.update({"legistar": _legistar, "civicclerk": _civicclerk, "boarddocs": _boarddocs, "elections": _elections,
                       "agenda_center": _agenda_center, "granicus": _granicus, "ecode360": _code("ecode360.com/"),
                       "amlegal": _code("/codes/")})

SITE_INCLUDE = {
    "school_district": ["board", "agenda", "minute", "meeting", "policy", "policies", "superintendent", "budget",
                        "millage", "news", "press", "election", "resolution", "district-office", "about",
                        "video", "transcript", "calendar-of-meetings", "boarddocs", "bond", "levy"],
    "government": ["council", "commission", "agenda", "minute", "ordinance", "resolution", "meeting",
                   "mayor", "clerk", "election", "qualif", "result", "runoff", "ballot", "news", "press",
                   "document", "board", "appoint", "transcript", "video"],
}
SITE_EXCLUDE = {
    "school_district": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/", "lunch", "menu",
                        "athletics", "sports", "staff-directory", "/students", "/parents", "powerschool",
                        "infinite-campus", "registration", "enroll"],
    "government": ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/"],
}


def civic_site_source(j: dict, src: dict) -> dict:
    """Official websites: crawl the pages where records live, not the whole site."""
    if j.get("kind") not in CIVIC_KINDS:
        return src
    k = "school_district" if j["kind"] == "school_district" else "government"
    if not j.get("include"):
        src["include"] = list(SITE_INCLUDE[k])
    if not j.get("exclude"):
        src["exclude"] = list(SITE_EXCLUDE[k])
    if not j.get("max_pages"):
        src["max_pages"] = (80 if j["tier"] == "b" else 40) if k == "school_district" else (120 if j["tier"] == "a" else 60)
    return src


SITE_SOURCE_HOOKS.append(civic_site_source)


def sources_for(j: dict, defaults: dict | None = None) -> list[dict]:
    from ..targets import sources_for as _sources_for

    return _sources_for(j, defaults)


# ----------------------------------------------------------------------------- newsrooms and voter guides
def press_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Official newsrooms from config/press.yaml, crawled as press releases."""
    out = []
    for p in load_yaml(Path(root) / "config" / "press.yaml").get("press") or []:
        if p.get("enabled", True) and p.get("url"):
            out.append({"id": p["id"], "url": p["url"], "kind": "press_release", "tier": p.get("tier", "b"), "depth": 3,
                        "max_pages": p.get("max_pages", 80), "include": p.get("include") or ["press", "news"],
                        "exclude": p.get("exclude") or ["/search", "login", "?print", "/tag/", "/author/", "calendar"],
                        "since": (defaults or {}).get("since", "2016-01-01"), "target": "", "level": "state",
                        "target_name": p.get("name", p["id"])})
    return out


def guide_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Endorsement / scorecard / voter-guide organizations from config/guides.yaml (kind guide)."""
    out = []
    for g in load_yaml(Path(root) / "config" / "guides.yaml").get("guides") or []:
        if g.get("enabled", True) and g.get("url"):
            out.append({"id": g["id"], "url": g["url"], "kind": "guide", "tier": g.get("tier", "b"), "depth": 3,
                        "max_pages": g.get("max_pages", 60), "include": g.get("include") or ["endorse", "candidate"],
                        "exclude": g.get("exclude") or ["/search", "login", "?print", "/tag/", "/author/", "donate", "cart", "shop"],
                        "since": (defaults or {}).get("since", "2016-01-01"), "target": "", "level": "guide",
                        "target_name": g.get("name", g["id"]), "guide_name": g.get("name", g["id"]),
                        "guide_kind": g.get("kind", "")})
    return out


SOURCE_PROVIDERS.append(press_sources)
SOURCE_PROVIDERS.append(guide_sources)


def all_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    from ..targets import all_sources as _all

    return _all(root, defaults)
