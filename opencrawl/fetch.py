"""HTTP fetcher: every request passes the network guard, robots.txt, the block list and the rate limiter, and GETs
use a conditional-request cache (ETag / Last-Modified) so re-runs only download what changed.

Limits on what a server can make the crawler do
  - only public addresses (netguard): checked before the request and again at connect time
  - redirects are followed by hand, at most `max_redirects` hops; every hop is checked like a new request (guard,
    block list, robots.txt, spacing) and credentials or API-key headers never follow a redirect to another host
  - bodies are streamed and cut off at `max_bytes`; a response that takes longer than `max_seconds` in total is
    abandoned (a per-read timeout alone lets a server trickle bytes forever)
  - Retry-After and Crawl-delay are honoured, but never past the run's deadline: a wait that would end after it
    stops the source (OutOfTime) instead of sleeping
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from . import netguard, redact
from .blocked import Blocked, BlockList, looks_blocked
from .polite import ROBOTS_MAX_BYTES, RateLimiter, RobotsCache, TooLate, host_of, parse_retry_after


class OutOfTime(Exception):
    """The run's time budget is spent: raised at the next request so a long source stops promptly."""


class Disallowed(Exception):
    """robots.txt (or the site's response to it), the network guard or the crawler's own limits put this URL
    off-limits."""


__all__ = ["Fetcher", "FetchResult", "Disallowed", "Blocked", "OutOfTime", "TooLarge"]

REDIRECTS = (301, 302, 303, 307, 308)
# headers that identify the caller: dropped when a redirect leaves the host they were meant for
CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "x-goog-api-key", "api-key",
                      "x-auth-token", "x-csrf-token"}
MAX_CRAWL_DELAY = 600       # seconds; a host asking for longer between requests is not crawled (Disallowed)


class TooLarge(ValueError):
    """The response was bigger, or took longer, than the configured limits."""


@dataclass
class FetchResult:
    url: str                 # the URL the content came from (after redirects)
    status: int
    headers: dict
    content: bytes
    elapsed: float
    not_modified: bool = False
    requested_url: str = ""  # the URL asked for, when a redirect led elsewhere

    @property
    def text(self) -> str:
        enc = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip().strip("\"'") or enc
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:  # a charset Python doesn't know
            return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self):
        return json.loads(self.content)


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port) or (
        pa.hostname == pb.hostname and pa.scheme == "http" and pb.scheme == "https" and pa.port in (None, 80)
        and pb.port in (None, 443))


def _iter_body(r):
    """The decoded body in pieces as they arrive: one socket read at a time (read1), so the total-time limit is
    checked between reads even when a server trickles bytes slowly."""
    raw = getattr(r, "raw", None)
    if raw is not None and hasattr(raw, "read1"):
        while True:
            chunk = raw.read1(256 * 1024, decode_content=True)
            if not chunk:
                return
            yield chunk
    else:
        yield from r.iter_content(64 * 1024)


def _strip_credentials(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in CREDENTIAL_HEADERS}


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
        max_seconds: float = 300.0,
        max_redirects: int = 5,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.max_redirects = max_redirects
        self.max_retries = max_retries
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.session.max_redirects = max_redirects
        self.set_pool(pool_maxsize)
        self.limiter = RateLimiter(min_interval, sleep=sleep, host_intervals=host_intervals)
        self.robots = RobotsCache(self._robots_get, user_agent)
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
        """Size the connection pool to the number of concurrent workers, with the network guard on every connection.

        urllib3's default pool_maxsize is 10, so with more worker threads the extra ones discard and re-open
        connections every request. Sizing the pool to the worker count keeps one keep-alive connection per host
        hot. A small connect/read Retry smooths transient network blips without touching our own 429/503 and
        bot-protection handling (status_forcelist is empty on purpose; redirects are followed by hand).
        """
        from urllib3.util.retry import Retry

        self.pool_maxsize = max(10, int(pool_maxsize))
        retry = Retry(total=2, connect=2, read=1, status=0, redirect=0, backoff_factor=0.5,
                      status_forcelist=[], respect_retry_after_header=False)
        if hasattr(self.session, "mount"):
            adapter = netguard.guarded_adapter(pool_connections=self.pool_maxsize, pool_maxsize=self.pool_maxsize,
                                               max_retries=retry, pool_block=False)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ checks shared by every request
    stop_at: float | None = None   # monotonic time; set by the scheduler for the run's budget

    def _check_time(self) -> None:
        if self.stop_at is not None and time.monotonic() > self.stop_at:
            raise OutOfTime("run time budget used up")

    def _proxied(self, url: str) -> bool:
        try:
            proxies = requests.utils.get_environ_proxies(url) if getattr(self.session, "trust_env", True) else {}
            proxies = {**proxies, **(getattr(self.session, "proxies", None) or {})}
            return bool(requests.utils.select_proxy(url, proxies))
        except Exception:  # noqa: BLE001
            return False

    def guard(self, url: str) -> None:
        """Network guard for one URL (raises Disallowed). Names are resolved here only when a proxy will make the
        connection; direct connections are checked at connect time by the guarded adapter."""
        try:
            netguard.check_url(url, resolve=self._proxied(url))
        except netguard.BlockedAddress as exc:
            raise Disallowed(f"{redact.url(url)}: {exc}") from None

    def _admit(self, url: str, robots: bool = True) -> None:
        """Everything a URL must pass before a request: guard, block list, robots.txt (and its Crawl-delay)."""
        self.guard(url)
        blocked = self.blocklist.is_blocked(url)
        if blocked:
            raise Blocked(host_of(url), blocked["reason"])
        if robots and not self._is_api_endpoint(url):
            if not self.robots.allowed(url):
                raise Disallowed(redact.url(url))
            delay = self.robots.crawl_delay(url)
            if delay and delay > MAX_CRAWL_DELAY:
                raise Disallowed(f"{host_of(url)} asks for {delay:.0f}s between requests (Crawl-delay); "
                                 f"this crawler waits at most {MAX_CRAWL_DELAY}s")

    def _wait(self, url: str, robots: bool = True) -> None:
        delay = None if (not robots or self._is_api_endpoint(url)) else self.robots.crawl_delay(url)
        try:
            self.limiter.wait(url, delay, deadline=self.stop_at)
        except TooLate as exc:
            raise OutOfTime(str(exc)) from None

    def _backoff(self, seconds: float) -> None:
        """Sleep before a retry, unless that would end after the run's deadline."""
        if self.stop_at is not None and time.monotonic() + seconds > self.stop_at:
            raise OutOfTime(f"a {seconds:.0f}s back-off would end after the run's deadline")
        self._sleep(seconds)

    def _read_body(self, r, url: str, t0: float, max_bytes: int | None = None) -> bytes:
        limit = self.max_bytes if max_bytes is None else max_bytes
        declared = r.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > limit:
            r.close()
            raise TooLarge(f"response over {limit} bytes: {redact.url(url)}")
        chunks, size = [], 0
        for chunk in _iter_body(r):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                r.close()
                raise TooLarge(f"response over {limit} bytes: {redact.url(url)}")
            if time.monotonic() - t0 > self.max_seconds:
                r.close()
                raise TooLarge(f"response took over {self.max_seconds:.0f}s: {redact.url(url)}")
        return b"".join(chunks)

    def _send(self, method: str, url: str, headers: dict, body_kw: dict | None = None, max_bytes: int | None = None,
              robots: bool = True):
        """One request, following redirects by hand. Returns (final url, response, body, elapsed)."""
        hdrs = dict(headers)
        cur, method_now, kw = url, method, dict(body_kw or {})
        for hop in range(self.max_redirects + 1):
            if hop:
                self._admit(cur, robots=robots)
            self._check_time()
            self._wait(cur, robots)
            t0 = time.monotonic()
            try:
                r = self.session.request(method_now, cur, headers=hdrs, timeout=self.timeout, stream=True,
                                         allow_redirects=False, **kw)
            except netguard.BlockedAddress as exc:
                raise Disallowed(f"{redact.url(cur)}: {exc}") from None
            loc = r.headers.get("Location") if r.status_code in REDIRECTS else None
            if loc:
                r.close()
                self.limiter.record(cur, r.status_code, time.monotonic() - t0)
                nxt = urljoin(cur, loc.strip())
                if urlsplit(nxt).scheme.lower() not in ("http", "https"):
                    raise Disallowed(f"{redact.url(cur)} redirects to a non-web address")
                if not _same_origin(cur, nxt):
                    hdrs = _strip_credentials(hdrs)
                if r.status_code in (301, 302, 303) and method_now != "GET":
                    method_now, kw = "GET", {}
                cur = nxt
                continue
            body = self._read_body(r, cur, t0, max_bytes)
            return cur, r, body, time.monotonic() - t0
        raise Disallowed(f"more than {self.max_redirects} redirects from {redact.url(url)}")

    # ------------------------------------------------------------------ robots.txt
    def _robots_get(self, url: str) -> tuple[int, str]:
        """robots.txt itself: guarded, spaced, size-capped, redirects followed (RFC 9309 asks for five)."""
        self.guard(url)
        try:
            final, r, body, elapsed = self._send("GET", url, {}, max_bytes=ROBOTS_MAX_BYTES * 4, robots=False)
        except TooLarge:
            return 200, ""          # an absurd robots.txt is treated as empty (allow), like an unparseable one
        self.limiter.record(url, r.status_code, elapsed)
        reason = looks_blocked(r.status_code, body)
        if reason:
            self.blocklist.add(url, f"robots.txt: {reason}", self.source_id)
            raise Blocked(host_of(url), reason)
        text = body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
        return r.status_code, text

    def allowed(self, url: str) -> bool:
        try:
            self.guard(url)
        except Disallowed:
            return False
        return self.robots.allowed(url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self._cache_dir / f"{key}.meta.json", self._cache_dir / f"{key}.body"

    MEMO_TTL = 900            # seconds: the same page asked for twice in one run (probe, then crawl; seed, then crawl)
    MEMO_MAX_BYTES = 2_000_000

    # ------------------------------------------------------------------ GET
    def get(self, url: str, params: dict | None = None, headers: dict | None = None, use_cache: bool = True) -> FetchResult:
        full = requests.Request("GET", url, params=params).prepare().url
        self._admit(full)
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

    def _get(self, full: str, headers: dict | None, use_cache: bool) -> FetchResult:
        self._check_time()
        meta_p, body_p = self._cache_paths(full)
        req_headers = dict(headers or {})
        cached = None
        if use_cache and meta_p.exists() and body_p.exists():
            try:
                cached = json.loads(meta_p.read_text())
            except ValueError:
                cached = None
            if cached and cached.get("etag"):
                req_headers["If-None-Match"] = cached["etag"]
            if cached and cached.get("last_modified"):
                req_headers["If-Modified-Since"] = cached["last_modified"]
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._check_time()               # a retry never outlives the run's budget
            t0 = time.monotonic()
            try:
                final, r, body, elapsed = self._send("GET", full, req_headers)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._backoff(min(60, 2 ** attempt * 5))
                continue
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            self.limiter.record(final, r.status_code, elapsed, retry_after)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(final, reason, self.source_id)
                raise Blocked(host_of(final), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                self._backoff(min(600, retry_after or 2 ** attempt * 10))
                continue
            if r.status_code == 304 and cached:
                return FetchResult(final, 200, cached.get("headers", {}), body_p.read_bytes(), elapsed, not_modified=True,
                                   requested_url=full)
            if r.status_code == 200 and use_cache and (hdrs.get("etag") or hdrs.get("last-modified")):
                body_p.write_bytes(body)
                meta_p.write_text(json.dumps({
                    "etag": hdrs.get("etag"),
                    "last_modified": hdrs.get("last-modified"),
                    "headers": {"content-type": hdrs.get("content-type", "")},
                }))
            return FetchResult(final, r.status_code, hdrs, body, elapsed, requested_url=full)
        raise last_exc or RuntimeError(f"gave up on {redact.url(full)}")

    # ------------------------------------------------------------------ POST
    def post(self, url: str, data: dict | None = None, headers: dict | None = None, json_body=None) -> FetchResult:
        """A POST (form `data`, or JSON `json_body`) with the same checks as get() (guard, robots, block list,
        spacing, back-off, size and time limits); never cached. Used only for read-only endpoints that a site's
        own pages load this way (meeting lists, legislation searches)."""
        full = requests.Request("POST", url).prepare().url
        self._admit(full)
        self._check_time()
        body_kw = {"json": json_body} if json_body is not None else {"data": data or {}}
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._check_time()
            t0 = time.monotonic()
            try:
                final, r, body, elapsed = self._send("POST", full, dict(headers or {}), body_kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self.limiter.record(full, 599, time.monotonic() - t0)
                self._backoff(min(60, 2 ** attempt * 5))
                continue
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            self.limiter.record(final, r.status_code, elapsed, retry_after)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            reason = looks_blocked(r.status_code, body)
            if reason:
                self.blocklist.add(final, reason, self.source_id)
                raise Blocked(host_of(final), reason)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                self._backoff(min(600, retry_after or 2 ** attempt * 10))
                continue
            return FetchResult(final, r.status_code, hdrs, body, elapsed, requested_url=full)
        raise last_exc or RuntimeError(f"gave up on {redact.url(full)}")

    # ------------------------------------------------------------------ large files
    def download(self, url: str, dest: Path, max_bytes: int, headers: dict | None = None) -> dict:
        """Stream a large file (a meeting recording) to `dest` with every check get() makes, without holding it in
        memory. Returns {url, status, bytes, content_type}; nothing is left at `dest` unless the status is 200."""
        full = requests.Request("GET", url).prepare().url
        self._admit(full)
        dest = Path(dest)
        hdrs = dict(headers or {})
        cur = full
        for hop in range(self.max_redirects + 1):
            if hop:
                self._admit(cur)
            self._check_time()
            self._wait(cur)
            t0 = time.monotonic()
            try:
                r = self.session.get(cur, headers=hdrs, timeout=self.timeout, stream=True, allow_redirects=False)
            except netguard.BlockedAddress as exc:
                raise Disallowed(f"{redact.url(cur)}: {exc}") from None
            loc = r.headers.get("Location") if r.status_code in REDIRECTS else None
            if loc:
                r.close()
                nxt = urljoin(cur, loc.strip())
                if urlsplit(nxt).scheme.lower() not in ("http", "https"):
                    raise Disallowed(f"{redact.url(cur)} redirects to a non-web address")
                if not _same_origin(cur, nxt):
                    hdrs = _strip_credentials(hdrs)
                cur = nxt
                continue
            with r:
                self.limiter.record(cur, r.status_code, time.monotonic() - t0)
                ctype = r.headers.get("Content-Type", "")
                if r.status_code != 200:
                    return {"url": cur, "status": r.status_code, "bytes": 0, "content_type": ctype}
                declared = r.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise TooLarge(f"{redact.url(cur)}: {declared} bytes (limit {max_bytes})")
                size = 0
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        for chunk in _iter_body(r):
                            size += len(chunk)
                            if size > max_bytes:
                                raise TooLarge(f"{redact.url(cur)}: over {max_bytes} bytes")
                            if time.monotonic() - t0 > max(self.max_seconds, 1800):
                                raise TooLarge(f"{redact.url(cur)}: download took too long")
                            self._check_time()
                            fh.write(chunk)
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
                return {"url": cur, "status": 200, "bytes": size, "content_type": ctype}
        raise Disallowed(f"more than {self.max_redirects} redirects from {redact.url(full)}")

    def host(self, url: str) -> str:
        return host_of(url)


def launch_browser(playwright, **kwargs):
    """Kept for callers that need a bare browser; crawling code uses browser.session(), which adds the guard."""
    from .browser import launch

    return launch(playwright, **kwargs)
