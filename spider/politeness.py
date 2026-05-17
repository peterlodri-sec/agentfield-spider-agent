"""Crawl politeness: robots.txt cache + per-host token bucket.

Ported from rag-stack/scripts/crawler.py (lines 427-511) without
behavior changes. Both classes are intentionally self-contained — no
imports from the rest of ``spider/`` — so they're easy to unit-test.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.robotparser
from functools import lru_cache
from typing import Any

import aiohttp

# ─── env knobs ──────────────────────────────────────────────────────
ROBOTS_TTL_S = int(os.environ.get("CRAWL_ROBOTS_TTL_S", "3600"))
HOST_RPS = float(os.environ.get("CRAWL_HOST_RPS", "1.0"))
HOST_BURST = int(os.environ.get("CRAWL_HOST_BURST", "1"))
ROBOTS_USER_AGENT = os.environ.get("CRAWL_ROBOTS_UA", "agentfield-spider/0.1")
ROBOTS_DISABLE = os.environ.get("CRAWL_ROBOTS_DISABLE", "0") == "1"


# ─── url helpers ────────────────────────────────────────────────────
_HOST_RE = re.compile(r"^https?://([^/]+)", re.I)


@lru_cache(maxsize=2048)
def host_of(url: str) -> str:
    """Return the host (incl. port if present) for an http(s) URL.

    Returns "" if the URL isn't http(s). Caches because both the robots
    gate and the host bucket call this per fetch.
    """
    m = _HOST_RE.match(url)
    return m.group(1) if m else ""


# ─── robots.txt cache ──────────────────────────────────────────────
class RobotsCache:
    """In-memory robots.txt cache with TTL.

    ``allowed(url)`` returns False *only* if we have a fresh robots.txt
    that explicitly disallows the path. Network errors and 4xx responses
    are treated as "no robots policy in force, fetch is allowed" — the
    rule major search engines follow.

    Thread-unsafe by design (aiohttp event loop is single-threaded).
    """

    __slots__ = ("_entries", "_sess")

    def __init__(self, sess: aiohttp.ClientSession) -> None:
        self._entries: dict[
            str, tuple[float, urllib.robotparser.RobotFileParser | None]
        ] = {}
        self._sess = sess

    async def _load(
        self, host: str,
    ) -> urllib.robotparser.RobotFileParser | None:
        rp = urllib.robotparser.RobotFileParser()
        url = f"https://{host}/robots.txt"
        try:
            async with self._sess.get(
                url,
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"User-Agent": ROBOTS_USER_AGENT},
            ) as r:
                if r.status == 200:
                    body = await r.text()
                    rp.parse(body.splitlines())
                    return rp
                # 4xx -> "no robots in force" per RFC 9309 §2.3.1
                if 400 <= r.status < 500:
                    return None
        except Exception:
            pass
        # Transient outage -> allow. A flaky robots host shouldn't strand
        # the crawler.
        return None

    async def allowed(self, url: str) -> bool:
        if ROBOTS_DISABLE:
            return True
        host = host_of(url)
        if not host:
            return True
        ent = self._entries.get(host)
        now = time.time()
        if ent is None or (now - ent[0]) > ROBOTS_TTL_S:
            rp = await self._load(host)
            self._entries[host] = (now, rp)
            ent = self._entries[host]
        rp = ent[1]
        if rp is None:
            return True
        return rp.can_fetch(ROBOTS_USER_AGENT, url)


# ─── per-host token bucket ──────────────────────────────────────────
class HostBucket:
    """Per-host async token bucket. Trades a tiny bit of throughput for
    not getting 429'd / banned.

    ``HOST_RPS`` defaults to 1.0 → one request per second per host.
    Bursts of ``HOST_BURST`` tokens are allowed before throttling kicks
    in (default 1 = strict serial per host).
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        # host -> [tokens, last_refill_ts, lock]
        self._state: dict[str, list[Any]] = {}

    async def acquire(self, host: str) -> None:
        if not host or HOST_RPS <= 0:
            return
        if host not in self._state:
            self._state[host] = [
                float(HOST_BURST), time.monotonic(), asyncio.Lock(),
            ]
        st = self._state[host]
        async with st[2]:
            now = time.monotonic()
            tokens, last, _ = st
            tokens = min(HOST_BURST, tokens + (now - last) * HOST_RPS)
            if tokens < 1.0:
                wait = (1.0 - tokens) / HOST_RPS
                # Tiny jitter (≤25 ms) so simultaneous bursts don't all
                # wake on the exact same tick.
                await asyncio.sleep(wait + (now % 0.025))
                tokens = 1.0
            st[0] = tokens - 1.0
            st[1] = time.monotonic()


__all__ = [
    "HOST_BURST",
    "HOST_RPS",
    "HostBucket",
    "ROBOTS_DISABLE",
    "ROBOTS_TTL_S",
    "ROBOTS_USER_AGENT",
    "RobotsCache",
    "host_of",
]
