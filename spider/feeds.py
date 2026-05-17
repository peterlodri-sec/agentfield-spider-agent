"""RSS / Atom feed polling.

Feeds are registered via ``POST /feeds`` (route handler in
``spider/routes.py``), then the poller loop in this module wakes on a
schedule and re-fetches each feed when its ``interval_s`` elapses.

New entries are SSRF-gated (layer 1) and seen-checked before being
enqueued — the dispatcher worker picks them up via the standard
``crawler:queue`` path so they go through the same browser fetch +
politeness pipeline as direct ``/fetch`` calls.

Ported from rag-stack/scripts/crawler.py lines 790-927 with topic
polling removed; the loop is now feed-only.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import aiohttp
import feedparser
import orjson

from .log import log
from .politeness import host_of
from .security import is_safe_external_url, is_safe_external_url_with_dns
from .state import K_FEEDS, enqueue, is_seen

# ─── env knobs ──────────────────────────────────────────────────────
POLL_TICK_MIN = int(os.environ.get("POLL_TICK_MIN", "30"))
POLL_TICK_MAX = int(os.environ.get("POLL_TICK_MAX", "600"))
DEFAULT_INTERVAL = int(os.environ.get("FEED_INTERVAL", "3600"))

_FEED_BODY_CAP_BYTES = 4 * 1024 * 1024  # 4 MiB
_FEED_FETCH_TIMEOUT_S = 30
_FEED_USER_AGENT = "agentfield-spider/0.1"


# ─── feed CRUD ──────────────────────────────────────────────────────
async def feed_add(
    redis: Any,
    *,
    url: str,
    name: str | None = None,
    interval_s: int | None = None,
) -> dict[str, Any]:
    """Register a feed. Caller must have already SSRF-gated ``url``."""
    feed_id = uuid.uuid4().hex[:8]
    feed = {
        "id": feed_id,
        "url": url,
        "name": (name or host_of(url))[:128] or url[:128],
        "interval_s": int(interval_s or DEFAULT_INTERVAL),
        "last_polled": 0,
    }
    await redis.hset(K_FEEDS, feed_id, orjson.dumps(feed))
    log(
        "feed+",
        id=feed_id, url=url, interval_s=feed["interval_s"],
    )
    return feed


async def feed_list(redis: Any) -> list[dict[str, Any]]:
    """Return all feeds, newest-poll first."""
    raw = await redis.hgetall(K_FEEDS)
    feeds = []
    for v in raw.values():
        try:
            feeds.append(orjson.loads(v))
        except orjson.JSONDecodeError:
            continue
    feeds.sort(key=lambda f: f.get("last_polled", 0), reverse=True)
    return feeds


async def feed_del(redis: Any, feed_id: str) -> bool:
    """Return True if a feed was deleted."""
    n = await redis.hdel(K_FEEDS, feed_id)
    if n:
        log("feed-", id=feed_id)
        return True
    return False


async def feed_get(redis: Any, feed_id: str) -> dict[str, Any] | None:
    raw = await redis.hget(K_FEEDS, feed_id)
    if raw is None:
        return None
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None


# ─── poll one feed ──────────────────────────────────────────────────
async def poll_feed(
    http: aiohttp.ClientSession,
    redis: Any,
    feed_id: str,
    feed: dict[str, Any],
) -> dict[str, Any]:
    """Fetch the feed URL with ETag/If-Modified-Since, parse, enqueue
    new entries that pass the SSRF gate and aren't already seen.

    Updates ``feed.last_polled``, ``last_etag``, ``last_modified`` and
    writes the feed back to ``crawler:feeds``.

    Returns a poll report: ``{found, new, status, error?}``.
    """
    url = feed["url"]

    if not await is_safe_external_url_with_dns(url):
        log("feed.ssrf-blocked", id=feed_id, url=url)
        return {"new": 0, "found": 0, "error": "feed url failed SSRF gate"}

    headers = {"User-Agent": _FEED_USER_AGENT}
    if feed.get("last_etag"):
        headers["If-None-Match"] = feed["last_etag"]
    if feed.get("last_modified"):
        headers["If-Modified-Since"] = feed["last_modified"]

    t0 = time.perf_counter()
    body = b""
    etag = None
    modified = None
    status = 0
    try:
        async with http.get(
            url,
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=_FEED_FETCH_TIMEOUT_S),
        ) as resp:
            status = resp.status
            if 300 <= resp.status < 400:
                ms = int((time.perf_counter() - t0) * 1000)
                log(
                    "feed.redirect-blocked", id=feed_id, url=url,
                    status=resp.status,
                    location=resp.headers.get("Location", ""), ms=ms,
                )
                return {
                    "new": 0, "found": 0, "status": resp.status,
                    "error": "feed url returned redirect",
                }
            if resp.status == 304:
                feed["last_polled"] = int(time.time())
                await redis.hset(K_FEEDS, feed_id, orjson.dumps(feed))
                log(
                    "feed", id=feed_id, url=url, status=304,
                    ms=int((time.perf_counter() - t0) * 1000),
                )
                return {"new": 0, "found": 0, "status": 304}
            body = await resp.content.read(_FEED_BODY_CAP_BYTES + 1)
            if len(body) > _FEED_BODY_CAP_BYTES:
                log("feed.too-large", id=feed_id, url=url, bytes=len(body))
                body = b""
            etag = resp.headers.get("ETag")
            modified = resp.headers.get("Last-Modified")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log("error", where="feed", id=feed_id, msg=err)
        return {"new": 0, "found": 0, "error": err}

    parsed = await asyncio.to_thread(feedparser.parse, body)
    entries = parsed.entries or []
    new = 0
    for entry in entries:
        link = entry.get("link")
        if not link or not isinstance(link, str):
            continue
        if not is_safe_external_url(link):
            log("feed.entry-blocked", id=feed_id, url=link)
            continue
        if await is_seen(redis, link):
            continue
        await enqueue(redis, link, source=f"feed:{feed_id}")
        new += 1

    feed["last_polled"] = int(time.time())
    if etag:
        feed["last_etag"] = etag
    if modified:
        feed["last_modified"] = modified
    await redis.hset(K_FEEDS, feed_id, orjson.dumps(feed))

    ms = int((time.perf_counter() - t0) * 1000)
    log(
        "feed", id=feed_id, url=url, status=status,
        found=len(entries), new=new, ms=ms,
    )
    return {"new": new, "found": len(entries), "status": status}


# ─── poller loop ────────────────────────────────────────────────────
async def poller(http: aiohttp.ClientSession, redis: Any) -> None:
    """Adaptive feed poller. Wakes when the next-due feed becomes due,
    clamped to ``[POLL_TICK_MIN, POLL_TICK_MAX]``. Empty registry =>
    sleep POLL_TICK_MAX.

    Cancel-safe.
    """
    log("poller", state="ready", min_s=POLL_TICK_MIN, max_s=POLL_TICK_MAX)
    try:
        while True:
            try:
                feeds = await redis.hgetall(K_FEEDS)
                if not feeds:
                    await asyncio.sleep(POLL_TICK_MAX)
                    continue
                now = int(time.time())
                next_due = float("inf")
                for fid_b, raw in feeds.items():
                    try:
                        feed = orjson.loads(raw)
                    except orjson.JSONDecodeError:
                        continue
                    interval = int(feed.get("interval_s", DEFAULT_INTERVAL))
                    last = int(feed.get("last_polled", 0))
                    due = last + interval
                    if due <= now:
                        fid = (
                            fid_b.decode("ascii")
                            if isinstance(fid_b, bytes)
                            else str(fid_b)
                        )
                        await poll_feed(http, redis, fid, feed)
                    elif due < next_due:
                        next_due = due
                if next_due == float("inf"):
                    sleep_for: float = POLL_TICK_MAX
                else:
                    sleep_for = max(
                        POLL_TICK_MIN,
                        min(POLL_TICK_MAX, next_due - int(time.time())),
                    )
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover
                log("error", where="poller", msg=f"{type(e).__name__}: {e}")
                await asyncio.sleep(POLL_TICK_MIN)
    except asyncio.CancelledError:
        log("poller", state="stopped")
        raise


__all__ = [
    "DEFAULT_INTERVAL", "POLL_TICK_MAX", "POLL_TICK_MIN",
    "feed_add", "feed_del", "feed_get", "feed_list",
    "poll_feed", "poller",
]
