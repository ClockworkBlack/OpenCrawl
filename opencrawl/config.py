"""Project root and settings.

A *project* is a directory with config/, data/, state/ and imports/ (see `opencrawl init`). The root is, in order:
  1. $OPENCRAWL_ROOT
  2. the current directory, when it holds config/settings.yaml
  3. the repository this package was installed from (a git checkout works as a project out of the box)

Environment variables
  OPENCRAWL_ROOT             project directory (above)
  OPENCRAWL_DB               path of the SQLite database (default <root>/data/crawl.db)
  OPENCRAWL_USER_AGENT       overrides http.user_agent
  OPENCRAWL_CONTACT          a contact address added to the User-Agent (a project or role address, never a personal
                             one); nothing is added unless this or http.contact is set
  OPENCRAWL_BROWSER_CHANNEL  "chrome" to use an installed Chrome instead of Playwright's Chromium
  OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX  1 to let the browser run without its sandbox when it can't start with it (root)
  OPENCRAWL_ALLOW_PRIVATE_NETWORKS    1 to let the crawler reach private and loopback addresses (see netguard.py)
  OPENCRAWL_REDACT_LOGS      1 to print subject ids as short hashes in logs (shared CI logs)
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _find_root() -> Path:
    env = os.environ.get("OPENCRAWL_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if (cwd / "config" / "settings.yaml").exists():
        return cwd
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "config" / "settings.yaml").exists():
        return checkout
    return cwd   # no project yet: `opencrawl init .` creates one here


ROOT = _find_root()

DEFAULTS: dict = {
    "http": {"user_agent": "OpenCrawl/1.0 (+https://github.com/your-org/your-repo)", "min_interval_seconds": 3,
             "timeout_seconds": 30, "max_bytes": 25_000_000, "max_response_seconds": 300, "max_redirects": 5,
             "blocked_ttl_days": 7, "host_intervals": {}, "api_endpoints": [], "contact": "",
             "allow_private_networks": False, "allow_hosts": []},
    "schedule": {"timezone": "UTC", "api_anytime": True, "windows": []},
    "cadence_days": {"a": 1, "b": 7, "c": 30},
    "frontier": {"document_ttl_days": 3650, "page_revisit_days": 7},
    "workers": 8,
    "recheck_days": 14,
    "max_minutes": 300,
    "limits": {"since": "2016-01-01", "max_pages": 200, "meeting_lookback_days": 45},
    "crawl": {},            # hot_links / cold_links / tag_kinds (see settings.yaml)
    "evidence": {},         # primary_suffixes / primary_domains
    "subjects": {},
    "derivers": None,       # None = the built-in civic derivers; [] = none; or a list of module paths
    "openrecords": {"enabled": False},
    "privacy": {"retention_days": None, "public_figures_only": True, "export_quarantine": False,
                "export_screenshot_text": True},
    "data_dir": "data",
    "state_dir": "state",
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(path: Path | None = None, root: Path | None = None) -> dict:
    """Settings with defaults filled in; data_dir / state_dir resolved against the project root."""
    root = Path(root) if root else ROOT
    p = path or root / "config" / "settings.yaml"
    raw = yaml.load(p.read_text(encoding="utf-8"), Loader=_LOADER) if p.exists() else {}
    settings = _merge(DEFAULTS, raw or {})
    settings["root"] = root
    settings["data_dir"] = root / settings["data_dir"]
    settings["state_dir"] = root / settings["state_dir"]
    if os.environ.get("OPENCRAWL_DB"):
        settings["db_path"] = Path(os.environ["OPENCRAWL_DB"])
    return settings


def make_store(cfg: dict):
    from .store import Store

    return Store(cfg.get("db_path") or cfg["data_dir"])


def make_fetcher(cfg: dict):
    from . import netguard
    from .fetch import Fetcher

    http = cfg.get("http", {})
    netguard.configure(bool(http.get("allow_private_networks")), list(http.get("allow_hosts") or []))
    return Fetcher(
        user_agent=_user_agent(http),
        state_dir=cfg["state_dir"],
        min_interval=float(http.get("min_interval_seconds", 2.0)),
        timeout=float(http.get("timeout_seconds", 30)),
        max_bytes=int(http.get("max_bytes", 25_000_000)),
        max_seconds=float(http.get("max_response_seconds", 300)),
        max_redirects=int(http.get("max_redirects", 5)),
        blocked_ttl_days=int(http.get("blocked_ttl_days", 7)),
        host_intervals=http.get("host_intervals") or {},
        api_endpoints=http.get("api_endpoints") or [],
    )


def _user_agent(http: dict) -> str:
    """Honest User-Agent. A contact address is added only when one is set on purpose (OPENCRAWL_CONTACT or
    http.contact): Wikimedia asks automated clients for one, but an address is never borrowed from elsewhere
    (the records mailbox, git config), so nothing identifying goes out unless the operator chose it."""
    ua = os.environ.get("OPENCRAWL_USER_AGENT") or http.get("user_agent") or DEFAULTS["http"]["user_agent"]
    contact = os.environ.get("OPENCRAWL_CONTACT") or http.get("contact")
    if contact and contact not in ua:
        ua = ua[:-1] + f"; {contact})" if ua.endswith(")") else f"{ua} ({contact})"
    return ua
