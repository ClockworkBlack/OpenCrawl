"""HTTP fetcher: every request passes robots.txt, the rate limiter, and a
conditional-request cache (ETag / Last-Modified) so re-runs only download what
changed."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .blocked import Blocked, BlockList, looks_blocked
from .polite import RateLimiter, RobotsCache, host_of, parse_retry_after


class OutOfTime(Exception):
    """The run's time budget is spent: raised at the next request so a long source stops promptly."""


class Disallowed(Exception):
    """robots.txt (or the site's response to it) says this URL is off-limits."""


__all__ = ["Fetcher", "FetchResult", "Disallowed", "Blocked"]


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict
    content: bytes
    elapsed: float
    not_modified: bool = False

    @property
    def text(self) -> str:
        enc = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or enc
        return self.content.decode(enc, errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self):
        return json.loads(self.content)


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        state_dir: Path,
        min_interval: float = 2.0,
        timeout: float = 30.0,
        max_bytes: int = 25_000_000,
        max_retries: int = 3,
        session: requests.Session | None = None,
        sleep=time.sleep,
        blocked_ttl_days: int = 7,
        pool_maxsize: int = 32,
        host_intervals: dict | None = None,
        api_endpoints: list[str] | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.set_pool(pool_maxsize)
        self.limiter = RateLimiter(min_interval, sleep=sleep, host_intervals=host_intervals)
        self.robots = RobotsCache(self._raw_get_text, user_agent)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = self.state_dir / "http_cache"
        self._cache_dir.mkdir(exist_ok=True)
        self.blocklist = BlockList(self.state_dir / "blocked.json", blocked_ttl_days)
        self._memo: dict = {}                 # (url, headers) -> (monotonic time, FetchResult), see get()
        self._memo_lock = threading.Lock()
        self._tls = threading.local()  # per-worker source id, so block records name the right source
        # Documented public APIs whose operators invite programmatic use (with an identifying User-Agent and a
        # modest rate) even though robots.txt, which is written for crawlers indexing pages, disallows the path.
        # Spacing, back-off and the block list still apply. Configured explicitly in settings (http.api_endpoints).
        self.api_endpoints = [e.lower().rstrip("/") for e in (api_endpoints or [])]

    def _is_api_endpoint(self, url: str) -> bool:
        u = re.sub(r"^https?://", "", url.lower())
        return any(u == e or u.startswith(e + "?") or u.startswith(e + "/") for e in self.api_endpoints)

    @property
    def source_id(self) -> str | None:
        return getattr(self._tls, "source_id", None)

    @source_id.setter
    def source_id(self, value: str | None) -> None:
        self._tls.source_id = value

    def set_pool(self, pool_maxsize: int) -> None:
        """Size the connection pool to the number of concurrent workers.

        urllib3's default pool_maxsize is 10, so with more worker threads the
        extra ones discard and re-open connections every request ("Connection
        pool is full, discarding connection"). Sizing the pool to the worker
        count keeps one keep-alive connection per host hot, which is most of
        the per-request latency on a many-host crawl. A small connect/read
        Retry smooths transient network blips without touching our own 429/503
        and bot-protection handling (status_forcelist is empty on purpose).
        """
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.pool_maxsize = max(10, int(pool_maxsize))
        retry = Retry(total=2, connect=2, read=1, status=0, redirect=0, backoff_factor=0.5,
                      status_forcelist=[], respect_retry_after_header=False)
        adapter = HTTPAdapter(pool_connections=self.pool_maxsize, pool_maxsize=self.pool_maxsize,
                              max_retries=retry, pool_block=False)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # robots.txt itself is fetched directly (still rate limited)
    def _raw_get_text(self, url: str) -> tuple[int, str]:
        self.limiter.wait(url)
        t0 = time.monotonic()
        r = self.session.get(url, timeout=self.timeout)
        self.limiter.record(url, r.status_code, time.monotonic() - t0)
        reason = looks_blocked(r.status_code, r.content)
        if reason:
            self.blocklist.add(url, f"robots.txt: {reason}", self.source_id)
            raise Blocked(host_of(url), reason)
        return r.status_code, r.text

    def allowed(self, url: str) -> bool:
        return self.robots.allowed(url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self._cache_dir / f"{key}.meta.json", self._cache_dir / f"{key}.body"

    MEMO_TTL = 900            # seconds: the same page asked for twice in one run (probe, then crawl; seed, then crawl)
    MEMO_MAX_BYTES = 2_000_000

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, use_cache: bool = True) -> FetchResult:
        full = requests.Request("GET", url, params=params).prepare().url
        blocked = self.blocklist.is_blocked(full)
        if blocked:
            raise Blocked(host_of(full), blocked["reason"])
        if not self._is_api_endpoint(full) and not self.robots.allowed(full):
            raise Disallowed(full)
        memo_key = (full, tuple(sorted((headers or {}).items())))
        if use_cache:
            hit = self._memo.get(memo_key)
            if hit and time.monotonic() - hit[0] < self.MEMO_TTL:
                return dataclasses.replace(hit[1], not_modified=True, elapsed=0.0)   # same content as earlier this run
        res = self._get(full, headers, use_cache)
        if use_cache and res.status == 200 and len(res.content) <= self.MEMO_MAX_BYTES:
            with self._memo_lock:
                if len(self._memo) >= 2000:
                    for k in list(self._memo)[:500]:
                        self._memo.pop(k, None)
                self._memo[memo_key] = (time.monotonic(), res)
        return res

    stop_at: float | None = None   # monotonic time; set by the scheduler for the run's budget

    def _check_time(self) -> None:
        if self.stop_at is not None and time.monotonic() > self.stop_at:
            raise OutOfTime("run time budget used up")

    def _get(self, full: str, headers: dict | None, use_cache: bool) -> FetchResult:
        self._check_time()
        meta_p, body_p = self._cache_paths(full)
        req_headers = dict(headers or {})
        cached = None
        if use_cache and meta_p.exists() and body_p.exists():
            cached = json.loads(meta_p.read_text())
            if cached.get("etag"):
                req_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                req_headers["If-Modified-Since"] = cached["last_modified"]

        delay = self.robots.crawl_delay(full)
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._check_time()               # a retry never outlives the run's budget
            self.limiter.wait(full, delay)
            t0 = time.monotonic()
            try:
                r = self.session.get(full, headers=req_headers, timeout=self.timeout, stream=True)
                chunks, size = [], 0
                for chunk in r.iter_content(256 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        r.close()
                        raise ValueError(f"response over {self.max_bytes} bytes: {full}")
                body = b"".join(chunks)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._sleep(min(60, 2 ** attempt * 5))
                continue
            elapsed = time.monotonic() - t0
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            self.limiter.record(full, r.status_code, elapsed, retry_after)
            hdrs = {k.lower(): v for k, v in r.headers.items()}

            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(full, reason, self.source_id)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                self._sleep(min(600, retry_after or 2 ** attempt * 10))
                continue
            if r.status_code == 304 and cached:
                return FetchResult(full, 200, cached.get("headers", {}), body_p.read_bytes(), elapsed, not_modified=True)
            if r.status_code == 200 and use_cache and (hdrs.get("etag") or hdrs.get("last-modified")):
                meta_p.write_text(json.dumps({
                    "etag": hdrs.get("etag"),
                    "last_modified": hdrs.get("last-modified"),
                    "headers": {"content-type": hdrs.get("content-type", "")},
                }))
                body_p.write_bytes(body)
            return FetchResult(full, r.status_code, hdrs, body, elapsed)
        raise last_exc or RuntimeError(f"gave up on {full}")

    def post(self, url: str, data: dict | None = None, headers: dict | None = None, json_body=None) -> FetchResult:
        """A POST (form `data`, or JSON `json_body`) with the same politeness as get() (robots, rate limit, block
        list, back-off); never cached. Used only for read-only endpoints that a site's own pages load this way
        (BoardDocs meeting lists, the General Assembly's legislation search)."""
        full = requests.Request("POST", url).prepare().url
        blocked = self.blocklist.is_blocked(full)
        if blocked:
            raise Blocked(host_of(full), blocked["reason"])
        if not self._is_api_endpoint(full) and not self.robots.allowed(full):
            raise Disallowed(full)
        self._check_time()
        delay = self.robots.crawl_delay(full)
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._check_time()
            self.limiter.wait(full, delay)
            t0 = time.monotonic()
            try:
                if json_body is not None:
                    r = self.session.post(full, json=json_body, headers=dict(headers or {}), timeout=self.timeout)
                else:
                    r = self.session.post(full, data=data or {}, headers=dict(headers or {}), timeout=self.timeout)
                body = r.content
                if len(body) > self.max_bytes:
                    raise ValueError(f"response over {self.max_bytes} bytes: {full}")
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._sleep(min(60, 2 ** attempt * 5))
                continue
            elapsed = time.monotonic() - t0
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            self.limiter.record(full, r.status_code, elapsed, retry_after)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(full, reason, self.source_id)
                raise Blocked(host_of(full), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                self._sleep(min(600, retry_after or 2 ** attempt * 10))
                continue
            return FetchResult(full, r.status_code, hdrs, body, elapsed)
        raise last_exc or RuntimeError(f"gave up on {full}")

    def host(self, url: str) -> str:
        return host_of(url)


def launch_browser(playwright):
    """Chromium for script-rendered pages. OPENCRAWL_BROWSER_CHANNEL=chrome uses the machine's installed Chrome (GitHub's
    runners ship one), which saves downloading Playwright's browser on every run; falls back to the bundled build."""
    import os

    channel = os.environ.get("OPENCRAWL_BROWSER_CHANNEL", "").strip()
    if channel:
        try:
            return playwright.chromium.launch(channel=channel)
        except Exception:  # noqa: BLE001 — that channel isn't installed here
            pass
    return playwright.chromium.launch()
