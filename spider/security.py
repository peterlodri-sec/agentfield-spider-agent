"""SSRF defence — two-layer gate copied from rag-stack/scripts/security.py.

Layer 1 (sync, no I/O): ``is_safe_external_url(url)`` validates scheme +
literal host; rejects loopback / private / link-local / reserved IPs,
legacy IPv4 forms (uint32, octal, hex, short dotted), and an exact-match
list of internal service hostnames.

Layer 2 (async, DNS): ``is_safe_resolved_host(host, port)`` resolves
``host`` and runs every answer through the same private-range check.
Catches hostnames that publicly DNS-resolve to internal IPs.

The combined helper ``is_safe_external_url_with_dns`` runs both. Use
that at any entry point that's about to do real network I/O.

``follow_safe_redirects`` walks the 3xx chain with GET (matching the
downstream fetcher) and gates every hop. Pass a ``hop_policy`` callback
to layer in robots.txt + per-host RPS bucket checks per hop.

A residual DNS-rebinding TOCTOU window remains between this resolve and
the actual TCP connect. The proper TOCTOU-free fix is a custom aiohttp
Resolver — out of scope for v1.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

# Hostnames that resolve to internal services. Kept as a frozenset
# because the lookup is hot (every fetch).
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "host.docker.internal",
        # fleet service names (compose / k8s aliases)
        "control-plane",
        "spider-agent",
        "spider-http",
        "web-search-agent",
        "my-agent",
        "pr-enricher-agent",
        "data-engineer-agent",
        "redis",
        "postgres",
        "ollama",
    }
)


def is_safe_external_url(url: str) -> bool:
    """Return True iff ``url`` is safe to hand to an outbound fetcher.

    Rejects on parse failure rather than raising — callers are on a hot
    path and want a boolean.
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower().strip().rstrip(".")
    if not host:
        return False

    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    # Legacy IPv4 forms that ``ipaddress`` refuses but the system
    # resolver and ``socket.inet_aton`` accept:
    #   "2130706433"   uint32 form           -> 127.0.0.1
    #   "127.1"        short dotted form     -> 127.0.0.1
    #   "0177.0.0.1"   octal-prefixed octets -> 127.0.0.1
    #   "0x7f.0.0.1"   hex-prefixed octets   -> 127.0.0.1
    if ip is None:
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            ip = None

    if ip is not None:
        return not _ip_is_unsafe(ip)

    return host not in _BLOCKED_HOSTNAMES


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is in any range we refuse to fetch from."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def is_safe_resolved_host(host: str, port: int = 80) -> bool:
    """Layer-2 SSRF gate: resolve ``host`` and reject if any answer
    points at an unsafe address.

    Rejects on the first unsafe address even if other answers in the
    set are public — a DNS server that returns ``[1.2.3.4, 127.0.0.1]``
    cannot launder the loopback past us.

    Failures (gaierror, network-down, empty answer set) return False
    — fail closed.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for family, _typ, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if _ip_is_unsafe(ip):
            return False
        _ = family
    return True


async def is_safe_external_url_with_dns(url: str) -> bool:
    """Combined layer-1 + layer-2 gate. Use at fetch entry points."""
    if not is_safe_external_url(url):
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower().strip().rstrip(".")
    if not host:
        return False
    # Skip DNS for IP literals (layer 1 already validated them).
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        pass
    try:
        port = u.port
    except ValueError:
        return False
    if port is None:
        port = 443 if u.scheme == "https" else 80
    return await is_safe_resolved_host(host, port)


# ─── allowlist (browser_act agent navigation gate) ──────────────────
def _parse_host_list(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated host list into a normalized frozenset.

    Trims whitespace, lowercases, drops empty entries and trailing dots.
    """
    if not raw:
        return frozenset()
    parts = (p.strip().lower().rstrip(".") for p in raw.split(","))
    return frozenset(p for p in parts if p)


def env_agent_allowed_hosts() -> frozenset[str]:
    """Read ``SPIDER_AGENT_ALLOWED_HOSTS`` env into a normalized set.

    Empty / unset env means no allowlist (every host that passes the
    base SSRF gate is acceptable).
    """
    import os as _os

    return _parse_host_list(_os.environ.get("SPIDER_AGENT_ALLOWED_HOSTS"))


def is_host_allowed(url: str, allowed: frozenset[str] | None) -> bool:
    """Return True iff ``url``'s host is in ``allowed``.

    ``allowed=None`` or an empty set disables the gate (returns True).
    Suffix matches are supported via a leading dot: ``.example.com``
    matches ``example.com`` and any subdomain. Bare entries match
    exactly.

    This is an ADDITIONAL filter on top of ``is_safe_external_url`` —
    the SSRF gate is always authoritative for private / loopback ranges.
    """
    if not allowed:
        return True
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower().strip().rstrip(".")
    if not host:
        return False
    for entry in allowed:
        if entry.startswith("."):
            suffix = entry[1:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == entry:
            return True
    return False


DEFAULT_REDIRECT_HOPS = 5


async def follow_safe_redirects(
    sess: Any,
    url: str,
    *,
    max_hops: int = DEFAULT_REDIRECT_HOPS,
    per_hop_timeout_s: float = 8.0,
    hop_policy: Callable[[str], Awaitable[bool]] | None = None,
) -> str | None:
    """Walk an HTTP redirect chain with ``allow_redirects=False``,
    running ``is_safe_external_url_with_dns`` at every hop.

    Uses GET (not HEAD) so the chain we observe matches what the
    downstream fetcher will see (HEAD-vs-GET differential defence).
    Body bytes are discarded; we only inspect status + Location.

    Returns the final URL or ``None`` on any gate failure.

    ``hop_policy`` is invoked with each hop URL before the GET. Returning
    False aborts the walk. Side effects are allowed — the caller uses
    this for per-hop robots.txt + rate-limit bucket checks.

    ``sess`` is duck-typed as anything with an async ``get()`` that
    yields a context manager.
    """
    from urllib.parse import urljoin

    import aiohttp

    current = url
    for _hop in range(max_hops + 1):
        if not await is_safe_external_url_with_dns(current):
            return None
        if hop_policy is not None and not await hop_policy(current):
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=per_hop_timeout_s)
            async with sess.get(
                current,
                allow_redirects=False,
                timeout=timeout,
            ) as r:
                status = r.status
                if 300 <= status < 400:
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        return None
                    current = urljoin(current, loc)
                    continue
                return current
        except Exception:
            return None
    return None


__all__ = [
    "DEFAULT_REDIRECT_HOPS",
    "env_agent_allowed_hosts",
    "follow_safe_redirects",
    "is_host_allowed",
    "is_safe_external_url",
    "is_safe_external_url_with_dns",
    "is_safe_resolved_host",
]
