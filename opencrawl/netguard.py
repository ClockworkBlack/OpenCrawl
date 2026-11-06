"""Where the crawler may connect: public internet addresses only.

A page the crawler opens decides which links it follows, where a redirect goes and what a headless browser loads.
Without a guard, a hostile or compromised page could point the crawler at the machine it runs on (127.0.0.1, a
cloud metadata service at 169.254.169.254, a database on the private network) and the answer would land in the
records and the exported bundle. So every connection is checked, twice:

  check_url / check_host  before a request: http(s) only, no credentials in the URL, no internal host names
                          (localhost, *.internal, *.local, metadata.google.internal...), IP literals in any spelling
                          (2130706433, 0x7f.1, [::ffff:127.0.0.1]) must be public, and a name that resolves here must
                          resolve to public addresses only
  GuardedAdapter          at connect time, for requests made directly (no proxy): the name is resolved once, every
                          address must be public, and the socket connects to those vetted addresses only, so a DNS
                          answer that changes between the check and the connection (DNS rebinding) gets nowhere

"Public" means globally routable unicast (ipaddress.is_global), with addresses embedded in IPv6 transition forms
(IPv4-mapped, 6to4, NAT64) checked as the IPv4 address they carry.

A proxy configured by the operator (HTTPS_PROXY) is allowed: the proxy resolves the name, so only the before-request
checks apply to the target. The headless browser goes through browser.GuardProxy, which applies the same rules to
every connection the browser makes (redirects and subresources included).

Defaults: local, private-network and cloud-hosted sites can be crawled (http.allow_private_networks: true), so
you can point OpenCrawl at a site on your own machine, your intranet or a private cloud network. Cloud *metadata*
services (169.254.169.254, metadata.google.internal...) stay blocked: they hand out the machine's credentials and are
never a site anyone means to crawl (http.block_metadata: false lifts even that). For crawling the open web from a
machine that can reach things you wouldn't publish, set http.allow_private_networks: false: then only public internet
addresses are fetched, except hosts or networks you list in http.allow_hosts.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_NAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost",
    "metadata", "metadata.google.internal", "metadata.goog", "metadata.azure.com", "instance-data",
    "kubernetes", "kubernetes.default", "host.docker.internal", "gateway.docker.internal", "host.containers.internal",
})
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp", ".localdomain",
                    ".cluster.local", ".svc", ".in-addr.arpa", ".ip6.arpa", ".onion")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")

_policy = {"allow_private": True, "block_metadata": True, "hosts": frozenset(), "nets": ()}
METADATA_NETS = (ipaddress.ip_network("169.254.169.254/32"), ipaddress.ip_network("169.254.170.2/32"),
                 ipaddress.ip_network("100.100.100.200/32"), ipaddress.ip_network("fd00:ec2::254/128"))
METADATA_NAMES = frozenset({"metadata", "metadata.google.internal", "metadata.goog", "metadata.azure.com",
                            "instance-data", "instance-data.ec2.internal"})
_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 60


class BlockedAddress(Exception):
    """The URL or address is not on the public internet (or not http/https). Deliberately not an OSError, so the
    HTTP stack neither retries it nor reports it as a network failure."""


def configure(allow_private: bool = True, allow_hosts: list[str] | None = None, block_metadata: bool = True) -> None:
    """Called once from config.make_fetcher with the settings' http.allow_private_networks / http.allow_hosts."""
    hosts, nets = set(), []
    for h in allow_hosts or []:
        h = str(h).strip().lower().rstrip(".")
        if not h:
            continue
        try:
            nets.append(ipaddress.ip_network(h, strict=False))
        except ValueError:
            hosts.add(h)
    _policy.update(allow_private=bool(allow_private), block_metadata=bool(block_metadata), hosts=frozenset(hosts),
                   nets=tuple(nets))
    with _cache_lock:
        _cache.clear()


def private_allowed() -> bool:
    return _policy["allow_private"] or os.environ.get("OPENCRAWL_ALLOW_PRIVATE_NETWORKS", "").strip().lower() in (
        "1", "true", "yes")


# ----------------------------------------------------------------------------- addresses
def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip in NAT64 or ip in NAT64_LOCAL:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.teredo:
        return ip.teredo[1]
    return None


def _is_metadata(ip) -> bool:
    if ip.version == 6:
        v4 = _embedded_v4(ip)
        if v4 is not None:
            ip = v4
    return any(ip.version == n.version and ip in n for n in METADATA_NETS)


def address_allowed(ip) -> bool:
    """True for a globally routable unicast address (or one the settings allow)."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if any(ip in n for n in _policy["nets"]):
        return True
    if _policy["block_metadata"] and _is_metadata(ip):
        return False
    if private_allowed():
        return True
    if ip.version == 6:
        v4 = _embedded_v4(ip)
        if v4 is not None:
            return address_allowed(v4)
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified


def ip_literal(host: str):
    """The address a host string means when it is an IP literal in any spelling getaddrinfo accepts
    (dotted, decimal, octal, hex, short forms such as 127.1, bracketed IPv6), else None."""
    h = host.strip().strip("[]").split("%", 1)[0].rstrip(".")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if h and all(c in "0123456789abcdefxABCDEFX." for c in h) and any(c.isdigit() for c in h):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def _name_blocked(name: str) -> bool:
    return name in BLOCKED_NAMES or name.endswith(BLOCKED_SUFFIXES) or "." not in name


def _resolve(host: str, port: int | None) -> tuple[str, ...]:
    """Addresses the name resolves to (cached briefly). Raises socket.gaierror when it doesn't resolve."""
    key = f"{host}:{port or 0}"
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    addrs = tuple(dict.fromkeys(i[4][0] for i in infos))
    with _cache_lock:
        if len(_cache) > 4096:
            _cache.clear()
        _cache[key] = (now, addrs)
    return addrs


def check_host(host: str, port: int | None = None, resolve: bool = True) -> None:
    """Raise BlockedAddress unless `host` is a public name or address. A name that doesn't resolve here passes
    (a proxy may resolve it; a direct connection will fail on its own, or be stopped by GuardedAdapter)."""
    if not host:
        raise BlockedAddress("no host")
    name = host.strip().strip("[]").lower().rstrip(".")
    if name in _policy["hosts"]:
        return
    if _policy["block_metadata"] and name in METADATA_NAMES:
        raise BlockedAddress(f"{name} is a cloud metadata service")
    if private_allowed():
        ip = ip_literal(name)
        if ip is not None and not address_allowed(ip):
            raise BlockedAddress(f"{name} is a cloud metadata service")
        if resolve:
            try:
                bad = [a for a in _resolve(name, port) if not address_allowed(a)]
            except (socket.gaierror, UnicodeError):
                bad = []
            if bad:
                raise BlockedAddress(f"{name} resolves to a cloud metadata service ({bad[0]})")
        return
    ip = ip_literal(name)
    if ip is not None:
        if not address_allowed(ip):
            raise BlockedAddress(f"{name} is not a public address")
        return
    if _name_blocked(name):
        raise BlockedAddress(f"{name} is an internal host name")
    if not resolve:
        return
    try:
        addrs = _resolve(name, port)
    except (socket.gaierror, UnicodeError):
        return
    bad = [a for a in addrs if not address_allowed(a)]
    if bad:
        raise BlockedAddress(f"{name} resolves to a non-public address ({bad[0]})")


def check_url(url: str, resolve: bool = True) -> None:
    """Raise BlockedAddress unless the URL is http(s), carries no credentials and names a public host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BlockedAddress(f"malformed URL ({exc})") from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedAddress(f"scheme {parts.scheme or '(none)'!r} is not fetched")
    if parts.username is not None or parts.password is not None:
        raise BlockedAddress("credentials in the URL")
    check_host(parts.hostname or "", port, resolve)


def url_allowed(url: str, resolve: bool = True) -> bool:
    try:
        check_url(url, resolve)
        return True
    except BlockedAddress:
        return False


def public_addresses(host: str, port: int) -> list[str]:
    """Resolve `host` for a direct connection: every address must be public (an answer that mixes public and
    private addresses is refused outright). IP literals are checked as they are."""
    name = host.strip().strip("[]").rstrip(".")
    allowed_name = name.lower() in _policy["hosts"]
    lit = ip_literal(name)
    if lit is not None:
        if not allowed_name and not address_allowed(lit):
            raise BlockedAddress(f"{name} is not a public address")
        return [str(lit)]
    if not allowed_name and not private_allowed() and _name_blocked(name.lower()):
        raise BlockedAddress(f"{name} is an internal host name")
    from urllib3.util.connection import allowed_gai_family

    infos = socket.getaddrinfo(name, port, allowed_gai_family(), socket.SOCK_STREAM)
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    if not allowed_name:
        bad = [a for a in addrs if not address_allowed(a)]
        if bad:
            raise BlockedAddress(f"{name} resolves to a non-public address ({bad[0]})")
    return addrs


def connect_public(host: str, port: int, timeout=None, source_address=None, socket_options=None) -> socket.socket:
    """A TCP connection to a vetted public address of host:port (the peer is checked again once connected)."""
    from urllib3.util import connection as u3conn

    last: Exception | None = None
    for addr in public_addresses(host, port):
        try:
            sock = u3conn.create_connection((addr, port), timeout, source_address=source_address,
                                            socket_options=socket_options)
        except OSError as exc:
            last = exc
            continue
        try:
            peer = sock.getpeername()[0]
        except OSError as exc:
            sock.close()
            last = exc
            continue
        if host.strip("[]").lower().rstrip(".") not in _policy["hosts"] and not address_allowed(peer):
            sock.close()
            raise BlockedAddress(f"{host} connected to a non-public address ({peer})")
        return sock
    raise last or OSError(f"no address for {host}")


# ----------------------------------------------------------------------------- requests / urllib3 integration
def _guarded_classes():
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

    class _Guard:
        def _new_conn(self):  # type: ignore[override]
            if getattr(self, "proxy", None) is not None:
                return super()._new_conn()   # the operator's proxy; the target was checked before the request
            try:
                sock = connect_public(self._dns_host, self.port, self.timeout, self.source_address, self.socket_options)
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            except TimeoutError as exc:
                raise ConnectTimeoutError(self, f"Connection to {self.host} timed out. "
                                                f"(connect timeout={self.timeout})") from exc
            except OSError as exc:
                raise NewConnectionError(self, f"Failed to establish a new connection: {exc}") from exc
            sys.audit("http.client.connect", self, self.host, self.port)
            return sock

    class GuardedHTTPConnection(_Guard, HTTPConnection):
        pass

    class GuardedHTTPSConnection(_Guard, HTTPSConnection):
        pass

    class GuardedHTTPConnectionPool(HTTPConnectionPool):
        ConnectionCls = GuardedHTTPConnection

    class GuardedHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = GuardedHTTPSConnection

    return {"http": GuardedHTTPConnectionPool, "https": GuardedHTTPSConnectionPool}


_POOLS: dict = {}


def _pools() -> dict:
    if not _POOLS:
        _POOLS.update(_guarded_classes())
    return _POOLS


def guarded_adapter(**kwargs):
    """A requests HTTPAdapter whose direct connections only reach public addresses."""
    from requests.adapters import HTTPAdapter

    class GuardedAdapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            super().init_poolmanager(*a, **kw)
            self.poolmanager.pool_classes_by_scheme = _pools()

        def proxy_manager_for(self, proxy, **kw):
            manager = super().proxy_manager_for(proxy, **kw)
            if not proxy.lower().startswith("socks"):   # SOCKS pools have their own connection classes
                manager.pool_classes_by_scheme = _pools()
            return manager

    return GuardedAdapter(**kwargs)
