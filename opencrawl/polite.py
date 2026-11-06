"""Politeness layer: robots.txt, per-host adaptive rate limiting, off-peak windows.

Everything the crawler fetches goes through these checks. A site that says no
(robots.txt Disallow, 401/403 on robots.txt, 429/503 with Retry-After) gets
respected, not worked around.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_RETRY_AFTER = 900        # seconds: a Retry-After longer than this ends the run's work on that host instead
ROBOTS_MAX_BYTES = 512 * 1024  # RFC 9309: at least 500 KiB must be parsed; anything after that is ignored


class TooLate(Exception):
    """Waiting for this host's next slot would run past the deadline."""


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


# --------------------------------------------------------------------------- robots
class RobotsCache:
    """Caches one parsed robots.txt per host.

    Rules follow RFC 9309: 4xx (other than 401/403) = everything allowed,
    401/403 = treat as fully disallowed, 5xx/network error = disallowed until
    the next refresh (conservative).
    """

    def __init__(self, get, user_agent: str, ttl: int = 86400):
        self._get = get  # callable(url) -> (status:int, text:str)
        self.user_agent = user_agent
        self.ttl = ttl
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, bool]] = {}
        self._lock = threading.Lock()

    def _load(self, host: str):
        with self._lock:
            hit = self._cache.get(host)
            if hit and time.time() - hit[0] < self.ttl:
                return hit
        from .blocked import Blocked

        parser = urllib.robotparser.RobotFileParser()
        deny_all = False
        try:
            status, text = self._get(host + "/robots.txt")
        except Blocked:
            raise
        except Exception:
            status, text = 599, ""
        if status in (401, 403) or status >= 500:
            deny_all = True
            parser = None
        elif 400 <= status < 500:
            parser.parse([])  # no robots.txt -> allow all
        else:
            parser.parse(text.splitlines())
        entry = (time.time(), parser, deny_all)
        with self._lock:
            self._cache[host] = entry
        return entry

    def allowed(self, url: str) -> bool:
        _, parser, deny_all = self._load(host_of(url))
        if deny_all or parser is None:
            return False
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        _, parser, _ = self._load(host_of(url))
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    def sitemaps(self, url: str) -> list[str]:
        _, parser, _ = self._load(host_of(url))
        if parser is None:
            return []
        return list(parser.site_maps() or [])


# --------------------------------------------------------------------------- rate limiting
@dataclass
class _HostState:
    next_ok: float = 0.0
    multiplier: float = 1.0
    baseline_latency: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """Per-host spacing that slows down when a site looks strained.

    - base interval = max(configured min_interval, robots Crawl-delay)
    - 429/503 or latency > 3x baseline -> multiplier doubles (cap 16x)
    - each healthy response decays the multiplier back toward 1x
    """

    def __init__(self, min_interval: float = 2.0, max_multiplier: float = 16.0, sleep=time.sleep, clock=time.monotonic,
                 host_intervals: dict | None = None):
        self.min_interval = min_interval
        # per-host floors: APIs built for machine traffic can go faster; some services ask for slower
        self.host_intervals = {k.lower(): float(v) for k, v in (host_intervals or {}).items()}
        self.max_multiplier = max_multiplier
        self._hosts: dict[str, _HostState] = {}
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock

    def _state(self, host: str) -> _HostState:
        with self._lock:
            return self._hosts.setdefault(host, _HostState())

    def base_interval(self, host: str) -> float:
        netloc = host.split("://", 1)[-1].lower()
        for h, v in self.host_intervals.items():
            if netloc == h or netloc.endswith("." + h):
                return v
        return self.min_interval

    def interval(self, host: str, crawl_delay: float | None = None) -> float:
        st = self._state(host)
        base = max(self.base_interval(host), crawl_delay or 0.0)
        return base * st.multiplier

    def wait(self, url: str, crawl_delay: float | None = None, deadline: float | None = None) -> float:
        """Block until this host may be hit again. Safe across threads: each
        host hands out slots in order, so workers on the same host serialize.
        With a `deadline` (monotonic time), a slot later than it raises TooLate
        instead of sleeping (the slot is not taken)."""
        host = host_of(url)
        st = self._state(host)
        with st.lock:
            now = self._clock()
            slot = max(now, st.next_ok)
            if deadline is not None and slot > deadline:
                raise TooLate(f"{host}: next request allowed in {slot - now:.0f}s, after the run's deadline")
            st.next_ok = slot + self.interval(host, crawl_delay)
        delay = max(0.0, slot - self._clock())
        if delay:
            self._sleep(delay)
        return delay

    def record(self, url: str, status: int, latency: float, retry_after: float | None = None) -> None:
        st = self._state(host_of(url))
        strained = status in (429, 503)
        if st.baseline_latency is None and not strained and status < 400:
            st.baseline_latency = latency
        elif st.baseline_latency and latency > 3 * st.baseline_latency:
            strained = True
        if strained:
            st.multiplier = min(self.max_multiplier, st.multiplier * 2)
        else:
            st.multiplier = max(1.0, st.multiplier * 0.8)
            if st.baseline_latency and status < 400:
                st.baseline_latency = 0.9 * st.baseline_latency + 0.1 * latency
        if retry_after:
            st.next_ok = max(st.next_ok, self._clock() + min(retry_after, MAX_RETRY_AFTER))


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(min(int(value[:9]), 10 ** 8))
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        return max(0.0, (when - dt.datetime.now(when.tzinfo)).total_seconds())
    except Exception:
        return None


# --------------------------------------------------------------------------- off-peak windows
@dataclass
class Window:
    days: list[str]
    start: dt.time
    end: dt.time  # may be earlier than start -> wraps past midnight

    def contains(self, local: dt.datetime) -> bool:
        t = local.time()
        today = DAYS[local.weekday()]
        yesterday = DAYS[(local.weekday() - 1) % 7]
        if self.start <= self.end:
            return today in self.days and self.start <= t < self.end
        # wrapping window: evening part belongs to the start day,
        # early-morning part belongs to the previous day's window
        return (today in self.days and t >= self.start) or (yesterday in self.days and t < self.end)


@dataclass
class Schedule:
    """'Good times' to crawl: when the sites being crawled see the least human traffic."""

    tz: ZoneInfo
    windows: list[Window] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict) -> "Schedule":
        tz = ZoneInfo(cfg.get("timezone") or "UTC")
        wins = []
        for w in cfg.get("windows", []):
            days = [d.lower()[:3] for d in w.get("days", DAYS)]
            wins.append(Window(days, dt.time.fromisoformat(w["start"]), dt.time.fromisoformat(w["end"])))
        return cls(tz, wins)

    def is_good_time(self, now: dt.datetime | None = None) -> bool:
        if not self.windows:
            return True
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self.tz)
        return any(w.contains(local) for w in self.windows)

    def next_good_time(self, now: dt.datetime | None = None) -> dt.datetime:
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self.tz)
        probe = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
        for _ in range(7 * 24 * 4):  # 15-minute steps, one week
            if self.is_good_time(probe):
                return probe
            probe += dt.timedelta(minutes=15)
        return probe
