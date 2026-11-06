"""The headless browser, locked down.

Pages that only render with JavaScript (and screenshots) need a real browser, and a real browser loads whatever a
page asks for: redirects, scripts, frames, images, trackers, other hosts. So the browser never touches the network
itself. It talks only to GuardProxy, a small proxy inside this process that resolves every host the browser asks
for and refuses the connection unless every address is public (netguard), the same rule the HTTP fetcher follows.
Redirects, subresources and DNS answers that change between a check and a connection all go through it.

On top of that:
  - the Chromium sandbox stays on. Running as root (a container) needs it off: set
    OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX=1 to allow that fallback knowingly
  - the browser process gets a minimal environment (no API keys, tokens or mailbox passwords from this process)
  - downloads are refused, service workers blocked, QUIC and WebRTC's non-proxied UDP off, no permissions granted
  - known tracking and advertising hosts are never requested (the crawler shouldn't feed them, and they slow
    pages down); media and fonts are skipped unless a screenshot needs them
  - every connection is capped in bytes and time
"""
from __future__ import annotations

import contextlib
import os
import selectors
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

from . import netguard

TRACKERS = (
    "google-analytics.com", "googletagmanager.com", "analytics.google.com", "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "adservice.google.com", "connect.facebook.net", "facebook.com/tr", "hotjar.com",
    "hotjar.io", "segment.io", "segment.com", "mixpanel.com", "fullstory.com", "clarity.ms", "newrelic.com",
    "nr-data.net", "scorecardresearch.com", "quantserve.com", "taboola.com", "outbrain.com", "criteo.com",
    "adsrvr.org", "amazon-adsystem.com", "bing.com/bat", "bat.bing.com", "ads-twitter.com", "analytics.twitter.com",
    "static.ads-twitter.com", "linkedin.com/px", "snap.licdn.com", "px.ads.linkedin.com", "tiktok.com/i18n/pixel",
    "analytics.tiktok.com", "matomo.cloud", "siteimproveanalytics.com", "siteimprove.com", "crazyegg.com",
    "mouseflow.com", "luckyorange.com", "heap.io", "heapanalytics.com", "pendo.io", "intercom.io", "hubspot.com/ptq",
    "hs-analytics.net", "hs-scripts.com", "addthis.com", "sharethis.com", "chartbeat.com", "parsely.com",
)
LAUNCH_ARGS = [
    "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps", "--disable-sync",
    "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--metrics-recording-only",
    "--no-first-run", "--no-default-browser-check", "--no-pings", "--disable-breakpad",
    "--disable-features=InterestFeedContentSuggestions,MediaRouter,OptimizationHints,Translate,AutofillServerCommunication",
]
MAX_CONNECTION_BYTES = 100_000_000
CONNECT_TIMEOUT = 20
IDLE_TIMEOUT = 60
MAX_HEAD = 64 * 1024


def is_tracker(url: str) -> bool:
    """A known tracking/advertising host (or host/path) from TRACKERS."""
    try:
        u = urlsplit(url.lower())
    except ValueError:
        return False
    host, path = (u.hostname or ""), u.path or "/"
    for t in TRACKERS:
        th, _, tp = t.partition("/")
        if (host == th or host.endswith("." + th)) and (not tp or path.startswith("/" + tp)):
            return True
    return False


# ----------------------------------------------------------------------------- the guard proxy
def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(8192)
        if not chunk:
            raise ConnectionError("client closed before sending a request")
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise ConnectionError("request head too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


def _split_hostport(s: str, default: int) -> tuple[str, int]:
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "")
    return host, int(port) if port.isdigit() else default


def _upstream_proxy(host: str, port: int, https: bool) -> tuple[str, int, dict] | None:
    """The proxy this process would use for host:port (HTTPS_PROXY / HTTP_PROXY / NO_PROXY), if any."""
    import requests

    url = f"{'https' if https else 'http'}://{host}:{port}/"
    try:
        p = requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url))
    except Exception:  # noqa: BLE001
        p = None
    if not p:
        return None
    u = urlsplit(p if "://" in p else "http://" + p)
    if u.scheme not in ("http", ""):
        return None      # an https:// or socks proxy: not chained here, so connect directly (guarded)
    hdrs = {}
    if u.username:
        import base64
        from urllib.parse import unquote

        cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
        hdrs["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    return u.hostname or "", u.port or 80, hdrs


class GuardProxy:
    """A local HTTP/HTTPS proxy for the browser that only connects to public addresses (see the module docstring).
    Use as a context manager; `url` is what to hand the browser."""

    def __init__(self, max_bytes: int = MAX_CONNECTION_BYTES):
        self.max_bytes = max_bytes
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(128)
        self._closed = threading.Event()
        self._socks: set[socket.socket] = set()
        self._lock = threading.Lock()
        self.refused: list[str] = []          # host:port the browser asked for and didn't get (for tests and logs)
        self._thread = threading.Thread(target=self._accept, name="opencrawl-guard-proxy", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.getsockname()[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._closed.set()
        with contextlib.suppress(OSError):
            self._srv.close()
        with self._lock:
            socks = list(self._socks)
        for s in socks:
            with contextlib.suppress(OSError):
                s.close()

    def _track(self, s: socket.socket) -> socket.socket:
        with self._lock:
            self._socks.add(s)
        return s

    def _untrack(self, *socks) -> None:
        for s in socks:
            if s is None:
                continue
            with self._lock:
                self._socks.discard(s)
            with contextlib.suppress(OSError):
                s.close()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(self._track(client),), daemon=True).start()

    def _refuse(self, client: socket.socket, target: str, code: int = 403, why: str = "") -> None:
        if code == 403:
            self.refused.append(target)
        msg = {403: "Forbidden", 400: "Bad Request", 502: "Bad Gateway", 411: "Length Required"}.get(code, "Error")
        body = (why or msg).encode()[:200]
        with contextlib.suppress(OSError):
            client.sendall(f"HTTP/1.1 {code} {msg}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                           f"Connection: close\r\n\r\n".encode() + body)

    def _open(self, host: str, port: int, tunnel: bool) -> tuple[socket.socket, bool]:
        """A connection for host:port: through the upstream proxy when this process uses one (the target was
        checked by name/literal/resolution first), else straight to a vetted public address. Returns
        (socket, via_upstream_proxy)."""
        netguard.check_host(host, port, resolve=True)
        up = _upstream_proxy(host, port, tunnel)
        if up:
            phost, pport, phdrs = up
            s = socket.create_connection((phost, pport), CONNECT_TIMEOUT)
            if tunnel:
                req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n" + "".join(
                    f"{k}: {v}\r\n" for k, v in phdrs.items()) + "\r\n"
                s.sendall(req.encode())
                head, rest = _read_head(s)
                status = head.split(b"\r\n", 1)[0].split()
                if len(status) < 2 or status[1] != b"200":
                    s.close()
                    raise ConnectionError(f"upstream proxy refused CONNECT {host}:{port}")
            return s, True
        return netguard.connect_public(host, port, CONNECT_TIMEOUT), False

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        target = "?"
        try:
            client.settimeout(IDLE_TIMEOUT)
            head, rest = _read_head(client)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                return self._refuse(client, target, 400)
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, port = _split_hostport(target, 443)
                try:
                    upstream, _ = self._open(host, port, tunnel=True)
                except netguard.BlockedAddress as exc:
                    return self._refuse(client, f"{host}:{port}", 403, str(exc))
                except OSError as exc:
                    return self._refuse(client, f"{host}:{port}", 502, type(exc).__name__)
                self._track(upstream)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                self._pipe(client, upstream, both_ways=True)
                return
            u = urlsplit(target)
            if u.scheme.lower() != "http" or not u.hostname:
                return self._refuse(client, target, 400, "absolute http:// URLs only")
            host, port = u.hostname, u.port or 80
            hdrs = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in (
                "proxy-connection", "proxy-authorization", "connection", "keep-alive")]
            lower = {ln.split(":", 1)[0].strip().lower(): ln.split(":", 1)[1].strip() for ln in hdrs if ":" in ln}
            if "chunked" in lower.get("transfer-encoding", "").lower():
                return self._refuse(client, target, 411)
            length = int(lower.get("content-length", "0") or 0) if lower.get("content-length", "0").isdigit() else 0
            body = rest
            while len(body) < length:
                chunk = client.recv(min(65536, length - len(body)))
                if not chunk:
                    break
                body += chunk
                if len(body) > 10_000_000:
                    return self._refuse(client, target, 400, "request body too large")
            try:
                upstream, via_proxy = self._open(host, port, tunnel=False)
            except netguard.BlockedAddress as exc:
                return self._refuse(client, f"{host}:{port}", 403, str(exc))
            except OSError as exc:
                return self._refuse(client, f"{host}:{port}", 502, type(exc).__name__)
            self._track(upstream)
            if via_proxy:
                up = _upstream_proxy(host, port, False)
                extra = [f"{k}: {v}" for k, v in (up[2] if up else {}).items()]
                line = f"{method} {target} {version}"
            else:
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                line = f"{method} {path} {version}"
                extra = []
            out = "\r\n".join([line] + hdrs + extra + ["Connection: close", "", ""]).encode("latin-1")
            upstream.sendall(out + body[:length] if length else out)
            self._pipe(upstream, client, both_ways=False)
        except (OSError, ConnectionError, ValueError, UnicodeError):
            return
        finally:
            self._untrack(client, upstream)

    def _pipe(self, a: socket.socket, b: socket.socket, both_ways: bool) -> None:
        """Copy a->b (and b->a when both_ways) until either side closes, the connection goes quiet for
        IDLE_TIMEOUT, or max_bytes have passed."""
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        if both_ways:
            sel.register(b, selectors.EVENT_READ, a)
        moved = 0
        try:
            while not self._closed.is_set():
                events = sel.select(IDLE_TIMEOUT)
                if not events:
                    return
                for key, _ in events:
                    src, dst = key.fileobj, key.data
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
                    moved += len(data)
                    if moved > self.max_bytes:
                        return
        finally:
            sel.close()


# ----------------------------------------------------------------------------- browser
def _minimal_env(home: str) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8"}
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "FONTCONFIG_PATH", "FONTCONFIG_FILE", "TZ"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def no_sandbox_allowed() -> bool:
    return os.environ.get("OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes")


def launch(playwright, proxy: str | None = None, home: str | None = None):
    """Chromium with the sandbox on (see module docstring), a minimal environment and, given `proxy`, every
    connection through it. OPENCRAWL_BROWSER_CHANNEL=chrome uses the machine's installed Chrome (GitHub's runners ship
    one) instead of Playwright's bundled build."""
    home = home or tempfile.mkdtemp(prefix="opencrawl-browser-")
    kw = {"headless": True, "args": list(LAUNCH_ARGS), "env": _minimal_env(home), "chromium_sandbox": True,
          "downloads_path": home}
    if proxy:
        kw["proxy"] = {"server": proxy, "bypass": "<-loopback>"}
    channel = os.environ.get("OPENCRAWL_BROWSER_CHANNEL", "").strip()
    attempts = ([{"channel": channel}] if channel else []) + [{}]
    last: Exception | None = None
    for extra in attempts:
        try:
            return playwright.chromium.launch(**kw, **extra)
        except Exception as exc:  # noqa: BLE001 — that channel isn't installed, or the sandbox can't start
            last = exc
            if no_sandbox_allowed():
                try:
                    return playwright.chromium.launch(**{**kw, "chromium_sandbox": False}, **extra)
                except Exception as exc2:  # noqa: BLE001
                    last = exc2
    raise RuntimeError(
        "could not start the headless browser with its sandbox. Install Chrome or run `playwright install chromium`; "
        "when running as root (a container) set OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX=1 to allow running it without "
        f"the sandbox. ({type(last).__name__}: {str(last).splitlines()[0][:200] if last else ''})")


@contextlib.contextmanager
def session(fetcher=None, block: tuple[str, ...] = ("media", "font"), **context_kwargs):
    """A browser context whose every connection goes through GuardProxy. `block` lists Playwright resource types
    not to load ("image", "media", "font", "stylesheet"...); screenshots pass block=() to load everything but
    trackers. Yields the BrowserContext."""
    from playwright.sync_api import sync_playwright

    home = tempfile.mkdtemp(prefix="opencrawl-browser-")
    ua = context_kwargs.pop("user_agent", None) or (fetcher.user_agent if fetcher else None)
    with GuardProxy() as gp, sync_playwright() as p:
        browser = launch(p, proxy=gp.url, home=home)
        try:
            ctx = browser.new_context(user_agent=ua, accept_downloads=False, service_workers="block",
                                      ignore_https_errors=False, java_script_enabled=True, permissions=[],
                                      **context_kwargs)
            blocked_types = set(block)

            def route(r):
                req = r.request
                url = req.url
                scheme = url.split(":", 1)[0].lower()
                if scheme in ("data", "blob", "about"):
                    return r.continue_()
                if scheme not in ("http", "https") or req.resource_type in blocked_types or is_tracker(url) \
                        or not netguard.url_allowed(url, resolve=False):
                    return r.abort("blockedbyclient")
                return r.continue_()

            ctx.route("**/*", route)
            ctx.guard_proxy = gp                       # for callers and tests: what the proxy refused
            try:
                yield ctx
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
        finally:
            with contextlib.suppress(Exception):
                browser.close()
            import shutil

            shutil.rmtree(home, ignore_errors=True)


def goto(page, url: str, fetcher=None, timeout_ms: int = 60000):
    """Navigate with the fetcher's checks first (guard, block list, robots.txt, spacing). Returns the response."""
    if fetcher is not None:
        fetcher._admit(url)
        fetcher._wait(url)
    t0 = time.monotonic()
    resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    if fetcher is not None:
        fetcher.limiter.record(url, resp.status if resp else 599, time.monotonic() - t0)
    return resp
