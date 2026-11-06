"""Civic seeding: official websites for a whole state's jurisdictions, plus what only governments have.

  wikidata_websites   one SPARQL query for every county (P882) and place (P774) FIPS code in a state -> website
  seed_jurisdictions  the generic seed driver with that lookup (Wikidata by FIPS, then the Wikipedia infobox)
  seed_school_districts  districts by Wikidata label / Wikipedia search within the state
  find_elections_site    the elections office page or site an official home page links to (registered as an
                         inspector, so `portals.elections` is filled for every government target)

Settings (settings.yaml):
  civic.state_fips   two-digit state FIPS code the registry covers (e.g. "13"); needed by the Wikidata queries
  civic.state_name   the state's name, used to disambiguate Wikipedia searches
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import extract
from .. import seed as generic
from ..fetch import Fetcher
from ..seed import (INSPECTORS, WIKIDATA_SPARQL, WIKI_API, _alnum_s, _clean_site, inspect_site, wikipedia_website,  # noqa: F401
                    wikipedia_search_website)

STATE: dict = {"fips": "", "name": ""}


def configure(civic_cfg: dict | None) -> None:
    c = civic_cfg or {}
    STATE["fips"] = str(c.get("state_fips") or "")
    STATE["name"] = c.get("state_name") or ""


# ----------------------------------------------------------------------------- wikidata by FIPS
def wikidata_websites(fetcher: Fetcher, state_fips: str | None = None) -> dict[str, str]:
    """{fips: official website} for every place (P774) and county (P882) of a state with one on Wikidata."""
    fips = state_fips or STATE["fips"]
    if not fips:
        raise RuntimeError("civic.state_fips is not set in settings.yaml")
    query = f"""SELECT ?fips ?web WHERE {{
      {{ ?item wdt:P774 ?fips . FILTER(STRSTARTS(?fips,"{fips}")) }}
      UNION
      {{ ?item wdt:P882 ?fips . FILTER(STRSTARTS(?fips,"{fips}")) }}
      ?item wdt:P856 ?web .
    }}"""
    r = fetcher.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"}, use_cache=False)
    if r.status != 200:
        raise RuntimeError(f"wikidata HTTP {r.status}")
    out = {}
    for row in r.json()["results"]["bindings"]:
        f, web = row["fips"]["value"], row["web"]["value"]
        if f not in out:  # first listed website wins
            out[f] = web
    return out


# ----------------------------------------------------------------------------- elections office
ELECTIONS_LINK = re.compile(r"\b(elections?|voter\s+registration|board\s+of\s+elections|registrar|elections?\s*(&|and)\s*"
                            r"(voter\s+)?registration|vote\b)", re.I)
STATE_VOTER_SITES = re.compile(r"\bsos\.[a-z]{2}\.gov|[a-z]{2,}sos\.gov|sos\.state\.[a-z]{2}\.us|sos\.[a-z]{2}\.us|mvp\.sos|"
                               r"vote\.gov|govote[a-z]*\.|facebook|twitter|instagram|youtube", re.I)


def find_elections_site(home_html: str, home_url: str) -> str | None:
    """The elections office page or site linked from the official home page (county boards often have their own
    domain). Only links the page itself carries; official-looking hosts only."""
    soup = extract.soup_of(home_html)
    host = urlsplit(home_url).netloc.lower().removeprefix("www.")
    best = None
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = urljoin(home_url, a["href"]).split("#")[0]
        if not ELECTIONS_LINK.search(f"{label} {a['href']}") or href.lower().startswith("mailto:"):
            continue
        h = urlsplit(href).netloc.lower().removeprefix("www.")
        if h != host and not re.search(r"\.(gov|us|org)$", h):
            continue
        if STATE_VOTER_SITES.search(href):
            continue  # the state's voter page / social links, not the county's office
        score = (2 if re.search(r"elections?", label, re.I) else 1) + (1 if h != host else 0)
        if not best or score > best[0]:
            best = (score, href)
    return best[1] if best else None


def _elections_inspector(fetcher: Fetcher, html: str, url: str, out: dict) -> None:
    el = find_elections_site(html, url)
    if el:
        out.setdefault("portals", {})["elections"] = el


INSPECTORS.append(_elections_inspector)


# ----------------------------------------------------------------------------- drivers
def seed_jurisdictions(fetcher: Fetcher, jurisdictions: list[dict], state_path: Path, workers: int = 6,
                       only: str | None = None, refresh: bool = False, log=print, max_age_days: float | None = None,
                       deadline: float | None = None) -> dict:
    """Websites and portals for counties and municipalities: Wikidata by FIPS (one query), then Wikipedia."""
    wd: dict[str, str] = {}
    fetcher.stop_at = deadline      # the lookup stops at its next request once the budget is spent
    try:
        wd = wikidata_websites(fetcher)
        log(f"wikidata: {len(wd)} websites")
    except Exception as exc:  # noqa: BLE001
        log(f"wikidata unavailable ({exc}); falling back to Wikipedia")

    def find(f: Fetcher, j: dict, _log) -> tuple[str | None, str, dict]:
        extra: dict = {}
        website = _clean_site(j.get("website")) or _clean_site(wd.get(j.get("fips") or "")) or _clean_site(j.get("_title_website"))
        if website:
            return website, "override" if j.get("website") else "wikidata", extra
        if j.get("wikipedia"):
            try:
                website = wikipedia_website(f, j["wikipedia"])
                if website:
                    return website, "wikipedia", extra
            except Exception as exc:  # noqa: BLE001
                extra["wikipedia_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return None, "", extra

    return generic.seed(fetcher, jurisdictions, state_path, workers=workers, only=only, refresh=refresh, log=log,
                        max_age_days=max_age_days, deadline=deadline, find=find)


seed = seed_jurisdictions


def wikidata_school_districts(fetcher: Fetcher, state_fips: str | None = None) -> list[dict]:
    """Wikidata items in the state whose English label mentions schools, with their official websites:
    [{label, web, county}] (located in a county directly, or in a city within one)."""
    fips = state_fips or STATE["fips"]
    if not fips:
        raise RuntimeError("civic.state_fips is not set in settings.yaml")
    query = f"""SELECT ?item ?label ?web ?countyLabel WHERE {{
      ?county wdt:P882 ?fips . FILTER(STRSTARTS(?fips,"{fips}"))
      {{ ?item wdt:P131 ?county }} UNION {{ ?item wdt:P131 ?city . ?city wdt:P131 ?county }}
      ?item wdt:P856 ?web ; rdfs:label ?label . FILTER(LANG(?label)="en")
      FILTER(CONTAINS(LCASE(?label),"school"))
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}"""
    r = fetcher.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"}, use_cache=False)
    if r.status != 200:
        raise RuntimeError(f"wikidata HTTP {r.status}")
    out = []
    for row in r.json()["results"]["bindings"]:
        out.append({"label": row["label"]["value"], "web": row["web"]["value"],
                    "county": row.get("countyLabel", {}).get("value", "").removesuffix(" County")})
    return out


def seed_school_districts(fetcher: Fetcher, districts: list[dict], state_path: Path, workers: int = 6,
                          only: str | None = None, refresh: bool = False, log=print, max_age_days: float | None = None,
                          deadline: float | None = None) -> dict:
    """Websites and board portals (BoardDocs etc.) for school districts, into the same discovered.json."""
    wd: list[dict] = []
    fetcher.stop_at = deadline
    try:
        wd = wikidata_school_districts(fetcher)
        log(f"wikidata: {len(wd)} school items")
    except Exception as exc:  # noqa: BLE001
        log(f"wikidata unavailable ({exc}); falling back to Wikipedia search")

    def match(d: dict) -> str | None:
        names = [d["name"]] + list(d.get("aliases") or [])
        keys = {_alnum_s(n) for n in names}
        for row in wd:
            if _alnum_s(row["label"]) in keys and (not d.get("county") or not row["county"] or _alnum_s(row["county"]) == _alnum_s(d["county"])):
                return row["web"]
        # "X County School District" vs "X County Schools": same county, label mentions school and nothing else
        for row in wd:
            if row["county"] and _alnum_s(row["county"]) == _alnum_s(d.get("county", "")) and not d.get("city") \
                    and re.fullmatch(r"[a-z]+countyschool(s|district|system)?", _alnum_s(row["label"])):
                return row["web"]
        return None

    def find(f: Fetcher, d: dict, _log) -> tuple[str | None, str, dict]:
        extra: dict = {"kind": "school_district"}
        website = _clean_site(d.get("website")) or _clean_site(match(d) or "")
        if website:
            return website, "override" if d.get("website") else "wikidata", extra
        web, page = wikipedia_search_website(f, [d["name"]] + list(d.get("aliases") or []), STATE["name"])
        if web and re.search(r"school", page or "", re.I):
            extra["wikipedia"] = page
            return web, "wikipedia", extra
        return None, "", extra

    return generic.seed(fetcher, districts, state_path, workers=workers, only=only, refresh=refresh, log=log,
                        max_age_days=max_age_days, deadline=deadline, find=find)
