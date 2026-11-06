"""Targets and sources: what the crawler covers.

  config/sources.yaml    hand-written sources: any website, feed, API or portal, one entry each
  config/targets.yaml    targets: things to cover (an organization, a place, a campaign, a company, a cause...).
                         A target expands into one source per place its records live: its website (`<id>:site`),
                         its YouTube channel (`<id>:youtube`), a meeting portal or other structured system when an
                         adapter knows it (`<id>:legistar`, ...).
  state/discovered.json  what `opencrawl seed` found for each target (website, portals, feeds, API hints)
  config/overrides.yaml  hand edits that win over the registry and over discovery

Layers, lowest to highest priority: targets.yaml (and any registered provider, such as the civic jurisdiction
registry) -> discovered.json -> overrides.yaml.

Extension points (used by opencrawl.civic and the adapters; a project can add its own)
  TARGET_PROVIDERS   functions(root) -> list of targets, appended to targets.yaml
  SOURCE_PROVIDERS   functions(root, defaults) -> list of sources, appended to sources.yaml
  PORTAL_PATTERNS    name -> regex that finds that kind of portal link on a page (seed records the first hit)
  PORTAL_SOURCES     name -> function(target, base, url) -> source dict for that portal (or None)
  SITE_SOURCE_HOOKS  functions(target, source) -> source: adjust the `:site` source (include lists by target kind)
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import yaml

TIER_CADENCE_DAYS = {"a": 1, "b": 7, "c": 30}

# social / video channel links any target may carry; the civic module adds meeting portals and code libraries
PORTAL_PATTERNS: dict[str, re.Pattern] = {
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9.\-_]+/?", re.I),
    # channel links only (@handle, /channel/, /c/, /user/, or a bare youtube.com/name as in a site's social icons),
    # never a single video, playlist or embed
    "youtube": re.compile(r"https?://(?:www\.|m\.)?youtube\.com/(?:@[\w.\-]+|channel/[\w\-]+|c/[\w\-]+|user/[\w\-]+|"
                          r"(?!(?:watch|embed|playlist|shorts|live|results|feed|redirect|t|about|hashtag|channel|c|user|"
                          r"premium|kids|music|gaming|account|signin|logout|s|v|attribution_link)\b)[A-Za-z][\w\-]{2,})"
                          r"(?=[/?\"'#\s<>]|$)", re.I),
}
SOCIAL = {"facebook", "youtube"}

TARGET_PROVIDERS: list[Callable[[Path], list[dict]]] = []
SOURCE_PROVIDERS: list[Callable[[Path, dict | None], list[dict]]] = []
PORTAL_SOURCES: dict[str, Callable[[dict, dict, str], dict | None]] = {}
SITE_SOURCE_HOOKS: list[Callable[[dict, dict], dict]] = []

GENERIC_EXCLUDE = ["calendar.aspx?", "/search", "login", "?print", "/tag/", "/author/", "/cart", "/checkout", "mailto:"]


# ----------------------------------------------------------------------------- files
_YAML: dict[str, tuple[float, int, object]] = {}
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)   # libyaml: about 8x faster on big registry files


def yaml_text(text: str):
    return yaml.load(text, Loader=_LOADER)


def load_yaml(path: Path, default=None):
    """Parsed YAML, cached by file modification time (big registry files are read by several steps of one run);
    callers get their own copy, so changing the result never changes the cache."""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    st = path.stat()
    hit = _YAML.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        data = hit[2]
    else:
        data = yaml_text(path.read_text(encoding="utf-8"))
        _YAML[str(path)] = (st.st_mtime, st.st_size, data)
    return copy.deepcopy(data) or (default if default is not None else {})


def load_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def _providers_loaded():
    """Import the modules that register providers (the civic registry and the adapters); harmless when absent."""
    try:
        from .civic import registry as _civic  # noqa: F401
    except ImportError:
        pass
    try:
        from . import adapters as _adapters  # noqa: F401
    except ImportError:
        pass


# ----------------------------------------------------------------------------- targets
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def safe_id(value) -> bool:
    """Ids name files in the export (targets/<id>.json) and state; lower-case letters, digits, '.', '_' and '-' only."""
    return isinstance(value, str) and bool(SAFE_ID.match(value))


def _warn(msg: str) -> None:
    import sys

    print(f"opencrawl: {msg}", file=sys.stderr)


def tier_of(t: dict) -> str:
    return t.get("tier") or "b"


def targets(root: Path) -> list[dict]:
    """Every target: config/targets.yaml plus registered providers, each merged with what seed discovered and
    with hand overrides. Each carries id, name, kind, tier, website (if known), portals{}, feeds[]."""
    _providers_loaded()
    root = Path(root)
    discovered = load_json(root / "state" / "discovered.json")
    overrides = load_yaml(root / "config" / "overrides.yaml").get("targets", {}) or {}
    base = list(load_yaml(root / "config" / "targets.yaml").get("targets") or [])
    for provider in TARGET_PROVIDERS:
        base += provider(root) or []
    out, seen = [], set()
    for t in base:
        t = dict(t)
        if not t.get("id") or t["id"] in seen:
            continue
        if not safe_id(t["id"]):
            _warn(f"target id {str(t['id'])[:60]!r} skipped: use lower-case letters, digits, '.', '_' and '-'")
            continue
        seen.add(t["id"])
        t.setdefault("name", t["id"])
        t.setdefault("kind", "target")
        t.setdefault("portals", {})
        d = discovered.get(t["id"], {})
        t.update({k: v for k, v in d.items() if k != "portals"})
        t["portals"] = {**(d.get("portals") or {}), **(t["portals"] or {})}
        o = dict(overrides.get(t["id"], {}) or {})
        t["portals"] = {**t["portals"], **(o.pop("portals", {}) or {})}
        t.update(o)
        t["tier"] = t.get("tier") or tier_of(t)
        out.append(t)
    return out


def by_id(root: Path) -> dict[str, dict]:
    return {t["id"]: t for t in targets(root)}


# ----------------------------------------------------------------------------- sources
def site_source(t: dict, base: dict) -> dict:
    """The target's own website as a source: pages, documents and feeds, auto channel; link filters from the target."""
    src = {**base, "id": f"{t['id']}:site", "url": t["website"], "kind": t.get("site_kind", "page"), "depth": t.get("depth", 3),
           "coverage": True, "max_pages": t.get("max_pages", 120 if t["tier"] == "a" else 60),
           "include": list(t.get("include") or []), "exclude": list(t.get("exclude") or GENERIC_EXCLUDE)}
    for hook in SITE_SOURCE_HOOKS:
        src = hook(t, src) or src
    return src


def sources_for(t: dict, defaults: dict | None = None) -> list[dict]:
    """Expand one target into crawl sources (see module docstring)."""
    if t.get("enabled") is False:
        return []
    _providers_loaded()
    base = {"target": t["id"], "target_name": t.get("name", t["id"]), "tier": t["tier"],
            "since": (defaults or {}).get("since", "2016-01-01")}
    srcs: list[dict] = []
    portals = t.get("portals") or {}
    for name, url in portals.items():
        if not url:
            continue
        if name == "youtube":
            # why this channel is believed to be the target's own: a hand check, or a link on its official website
            basis = (f"hand-checked: {t['youtube_checked']}" if t.get("youtube_checked") else
                     f"linked from the official website ({t['website']})" if t.get("website") else "")
            srcs.append({**base, "id": f"{t['id']}:youtube", "url": url, "kind": "video", "channel": "api",
                         "endpoint": f"youtube:{url}", "youtube_basis": basis})
        elif name in PORTAL_SOURCES:
            s = PORTAL_SOURCES[name](t, base, url)
            if s:
                srcs.append(s)
    for i, feed in enumerate(t.get("extra_feeds") or []):
        srcs.append({**base, "id": f"{t['id']}:feed{i}", "url": feed, "kind": t.get("site_kind", "page"), "channel": "feed",
                     "endpoint": feed})
    if t.get("website"):
        srcs.append(site_source(t, base))
    for s in srcs:
        s.setdefault("kind", "page")
    return [s for s in srcs if s.get("url")]


def all_sources(root: Path, defaults: dict | None = None) -> list[dict]:
    """Target-derived sources, the registered providers' sources, and the hand-written ones in config/sources.yaml
    (which win on id clash)."""
    _providers_loaded()
    root = Path(root)
    hand = {s["id"]: s for s in (load_yaml(root / "config" / "sources.yaml").get("sources") or []) if s.get("id")}
    out = []
    for t in targets(root):
        for s in sources_for(t, defaults):
            out.append({**s, **hand.pop(s["id"], {})})
    for provider in SOURCE_PROVIDERS:
        for s in provider(root, defaults) or []:
            out.append({**s, **hand.pop(s["id"], {})})
    for s in hand.values():
        if s.get("enabled", True) and s.get("url"):
            s = dict(s)
            s.setdefault("kind", "page")
            s.setdefault("tier", "b")
            s.setdefault("since", (defaults or {}).get("since", "2016-01-01"))
            s.setdefault("target", "")
            out.append(s)
    return out


# ----------------------------------------------------------------------------- portals on a page
def find_portals(html: str, base_url: str) -> dict:
    """Links on a site that point at a known portal, structured system or official social account."""
    found = {}
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    for name, pat in PORTAL_PATTERNS.items():
        for m in pat.finditer(html):
            url = m.group(0).rstrip("/\\'\"")
            if name == "agenda_center" and urlsplit(url).netloc.lower().removeprefix("www.") != host:
                continue
            if name == "facebook" and re.search(r"facebook\.com/(sharer|share|plugins|dialog|login|tr\b)", url, re.I):
                continue
            found.setdefault(name, url)
            break
    return found
