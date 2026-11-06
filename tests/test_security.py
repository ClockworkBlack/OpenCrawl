"""The network guard, redirect handling, response limits, secret redaction and the guarded browser."""
import http.server
import socket
import threading
import time

import pytest
import requests

from opencrawl import netguard, redact
from opencrawl.fetch import Disallowed, Fetcher, OutOfTime, TooLarge


# ----------------------------------------------------------------------------- netguard
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8080/", "http://LOCALHOST./", "http://[::1]/", "http://0.0.0.0/",
    "http://10.0.0.5/", "http://172.16.3.4/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://[fe80::1]/", "http://[fc00::1]/", "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/", "http://[2002:7f00:1::]/", "http://[64:ff9b::a9fe:a9fe]/", "http://2130706433/",
    "http://0x7f000001/", "http://0x7f.1/", "http://127.1/", "http://017700000001/", "http://metadata.google.internal/",
    "http://printer/", "http://db.internal/", "http://nas.local/", "http://x.svc.cluster.local/", "http://224.0.0.1/",
    "file:///etc/passwd", "ftp://example.org/", "gopher://example.org/", "http://user:pw@example.org/",
    "javascript:alert(1)", "http://[::]/",
])
def test_guard_refuses_internal_and_odd_urls(guard_on, url):
    with pytest.raises(netguard.BlockedAddress):
        netguard.check_url(url, resolve=False)


@pytest.mark.parametrize("url", ["https://example.org/", "http://93.184.215.14/", "https://[2606:4700::6810:84e5]/",
                                 "https://sub.example.co.uk:8443/x?y=1"])
def test_guard_allows_public(guard_on, url):
    netguard.check_url(url, resolve=False)


def test_guard_resolution_and_allowlist(guard_on, monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ("93.184.215.14", "10.1.2.3"))
    with pytest.raises(netguard.BlockedAddress, match="non-public"):
        netguard.check_url("https://rebind.example.org/")      # one private answer is enough to refuse
    netguard.configure(False, ["rebind.example.org"])
    netguard.check_url("https://rebind.example.org/")
    netguard.configure(False, ["10.1.2.0/24"])
    assert netguard.address_allowed("10.1.2.3") and not netguard.address_allowed("10.1.3.3")
    netguard.configure(True, [])
    netguard.check_url("http://127.0.0.1/")


def _serve(handler, host="127.0.0.1"):
    srv = http.server.ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}"


def test_connect_time_guard_stops_direct_connections(guard_on, monkeypatch):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"internal")

        def log_message(self, *a):
            pass

    srv, base = _serve(H)
    port = srv.server_address[1]
    real = socket.getaddrinfo

    def rebinding(host, *a, **kw):          # a public-looking name whose DNS answer points at loopback
        return real("127.0.0.1", *a, **kw) if host == "rebind.example.org" else real(host, *a, **kw)

    try:
        s = requests.Session()
        s.trust_env = False
        s.mount("http://", netguard.guarded_adapter())
        with pytest.raises(netguard.BlockedAddress):
            s.get(base + "/", timeout=5)                         # the adapter alone refuses loopback
        monkeypatch.setattr(socket, "getaddrinfo", rebinding)
        netguard.check_url(f"http://rebind.example.org:{port}/", resolve=False)   # the name itself looks fine...
        with pytest.raises(netguard.BlockedAddress, match="non-public"):
            s.get(f"http://rebind.example.org:{port}/", timeout=5)               # ...the connection is refused
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- the fetcher's limits
class Site(http.server.BaseHTTPRequestHandler):
    seen_auth: list = []

    def do_GET(self):
        p = self.path
        Site.seen_auth.append((p, self.headers.get("X-Api-Key"), self.headers.get("Authorization")))
        if p == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private\n"
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        if p.startswith("/hop/"):
            n = int(p.split("/")[2])
            self.send_response(302); self.send_header("Location", f"/hop/{n + 1}"); self.end_headers(); return
        if p == "/to-private":
            self.send_response(302); self.send_header("Location", "/private/x"); self.end_headers(); return
        if p == "/to-other-host":
            self.send_response(307); self.send_header("Location", Site.other + "/landing"); self.end_headers(); return
        if p == "/to-file":
            self.send_response(302); self.send_header("Location", "file:///etc/passwd"); self.end_headers(); return
        if p == "/big":
            self.send_response(200); self.end_headers()
            for _ in range(64):
                self.wfile.write(b"x" * 65536)
            return
        if p == "/drip":
            self.send_response(200); self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"x"); self.wfile.flush(); time.sleep(0.1)
            except OSError:
                pass
            return
        if p == "/slow-retry":
            self.send_response(503); self.send_header("Retry-After", "86400"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html>ok " + p.encode() + b"</html>")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def sites():
    a, base_a = _serve(Site)
    b, base_b = _serve(Site, "127.0.0.2")
    Site.other = base_b
    yield base_a, base_b
    a.shutdown(); b.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None, max_bytes=1_000_000,
                   max_seconds=2)


def test_redirect_limits(sites, fetcher):
    a, b = sites
    with pytest.raises(Disallowed, match="redirects"):
        fetcher.get(a + "/hop/1")                                    # endless chain: stops after max_redirects
    with pytest.raises(Disallowed):
        fetcher.get(a + "/to-private")                               # a redirect into a robots-disallowed path
    with pytest.raises(Disallowed, match="non-web"):
        fetcher.get(a + "/to-file")
    Site.seen_auth.clear()
    r = fetcher.get(a + "/to-other-host", headers={"X-Api-Key": "s3cr3t"}, use_cache=False)
    assert r.status == 200 and r.url == b + "/landing" and r.requested_url == a + "/to-other-host"
    sent = {p: k for p, k, _ in Site.seen_auth}
    assert sent["/to-other-host"] == "s3cr3t" and sent["/landing"] is None   # the key never follows to another host


def test_redirect_into_private_network_is_refused(sites, fetcher, guard_on):
    a, b = sites
    netguard.configure(False, ["127.0.0.1"])                         # pretend 127.0.0.1 is public, 127.0.0.2 private
    with pytest.raises(Disallowed, match="not a public address"):
        fetcher.get(a + "/to-other-host", use_cache=False)


def test_size_and_time_limits(sites, fetcher):
    a, _ = sites
    with pytest.raises(TooLarge, match="bytes"):
        fetcher.get(a + "/big")
    t0 = time.monotonic()
    with pytest.raises(TooLarge, match="took over"):
        fetcher.get(a + "/drip")                                     # 1 byte / 0.1 s: cut off at max_seconds
    assert time.monotonic() - t0 < 6


def test_retry_after_never_outlives_the_deadline(sites, fetcher):
    a, _ = sites
    fetcher.stop_at = time.monotonic() + 30
    with pytest.raises(OutOfTime):
        fetcher.get(a + "/slow-retry")                               # Retry-After: 86400 -> stop, don't sleep


def test_robots_txt_size_cap(tmp_path):
    class Huge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            if self.path == "/robots.txt":
                try:
                    self.wfile.write(b"# padding\n" * 300_000)          # ~3 MB
                except OSError:
                    pass
            else:
                self.wfile.write(b"page")

        def log_message(self, *a):
            pass

    srv, base = _serve(Huge)
    try:
        s = requests.Session(); s.trust_env = False
        f = Fetcher("Test/1", tmp_path / "s", min_interval=0, session=s, sleep=lambda _: None)
        assert f.get(base + "/x").status == 200                     # an absurd robots.txt is ignored, not fatal
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- redaction
def test_redaction():
    assert redact.url("https://api.example.org/v1?api_key=abc123&q=x") == "https://api.example.org/v1?api_key=REDACTED&q=x"
    assert redact.url("https://u:p@example.org/a?token=t&key=k") == "https://example.org/a?token=REDACTED&key=REDACTED"
    msg = "HTTPSConnectionPool(host='api.x'): Max retries exceeded with url: /v1/c?api_key=SECRET (Caused by ...)"
    assert "SECRET" not in redact.text(msg)
    assert "tok" not in redact.text("Authorization: Bearer tok_abcdefghijkl")
    assert "zzz" not in redact.text("{'X-Api-Key': 'zzz'}")
    assert redact.clean_output("ok\n::set-output name=x::1\n\x1b[31mred").splitlines()[1].startswith("\u200b::")
    assert "\x1b" not in redact.clean_output("\x1b[31mred")


def test_blocklist_and_status_are_redacted(tmp_path):
    from opencrawl.blocked import BlockList
    from opencrawl.scheduler import Progress

    bl = BlockList(tmp_path / "blocked.json")
    bl.add("https://x.example.org/feed?token=SECRET", "HTTP 403 access denied")
    assert "SECRET" not in (tmp_path / "blocked.json").read_text()
    pr = Progress(tmp_path / "progress.json")
    pr.update("s", status="error: GET https://x.example.org/a?api_key=SECRET failed")
    assert "SECRET" not in (tmp_path / "progress.json").read_text()


# ----------------------------------------------------------------------------- the guarded browser
def test_browser_cannot_be_redirected_into_a_private_network(guard_on):
    pytest.importorskip("playwright")
    from opencrawl import browser

    class Secret(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"<html>TOPSECRET</html>")

        def log_message(self, *a):
            pass

    sb, base_b = _serve(Secret, "127.0.0.2")

    class Public(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redir":
                self.send_response(302); self.send_header("Location", base_b + "/secret"); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(f'<html><h1>Public</h1><img src="{base_b}/p.png"></html>'.encode())

        def log_message(self, *a):
            pass

    sa, base_a = _serve(Public)
    netguard.configure(False, ["127.0.0.1"])           # 127.0.0.1 plays a public site, 127.0.0.2 a private one
    try:
        with browser.session(None) as ctx:
            page = ctx.new_page()
            assert page.goto(base_a + "/", wait_until="load", timeout=20000).status == 200
            assert "Public" in page.content()
            try:
                page.goto(base_a + "/redir", wait_until="load", timeout=20000)
            except Exception:  # noqa: BLE001 — a refused navigation may also surface as an error
                pass
            assert "TOPSECRET" not in page.content()
            assert any(x.startswith("127.0.0.2:") for x in ctx.guard_proxy.refused)
    except RuntimeError as exc:
        pytest.skip(f"browser unavailable: {exc}")
    finally:
        sa.shutdown(); sb.shutdown()


def test_tracker_list():
    from opencrawl.browser import is_tracker

    assert is_tracker("https://www.googletagmanager.com/gtm.js?id=x") and is_tracker("https://www.facebook.com/tr?x")
    assert not is_tracker("https://www.facebook.com/townofexample") and not is_tracker("https://example.org/tr")


def test_guard_proxy_refuses_non_http_and_garbage():
    from opencrawl.browser import GuardProxy

    with GuardProxy() as gp:
        port = int(gp.url.rsplit(":", 1)[1])
        for req in (b"GET ftp://example.org/ HTTP/1.1\r\n\r\n", b"NONSENSE\r\n\r\n"):
            s = socket.create_connection(("127.0.0.1", port), 5)
            s.sendall(req)
            assert s.recv(100).startswith(b"HTTP/1.1 400")
            s.close()


def test_default_policy_allows_local_and_cloud_sites_but_not_metadata(monkeypatch):
    monkeypatch.delenv("OPENCRAWL_ALLOW_PRIVATE_NETWORKS", raising=False)
    netguard.configure()                                   # the defaults from settings.yaml
    try:
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://10.0.0.5/", "http://intranet/",
                    "http://app.internal/", "http://[::1]/"):
            netguard.check_url(url, resolve=False)
        for url in ("http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
                    "http://[fd00:ec2::254]/", "http://[::ffff:169.254.169.254]/", "file:///etc/passwd"):
            with pytest.raises(netguard.BlockedAddress):
                netguard.check_url(url, resolve=False)
        assert not netguard.address_allowed("169.254.169.254") and netguard.address_allowed("192.168.1.10")
        netguard.configure(True, [], block_metadata=False)
        netguard.check_url("http://169.254.169.254/", resolve=False)
    finally:
        netguard.configure(False, [])
