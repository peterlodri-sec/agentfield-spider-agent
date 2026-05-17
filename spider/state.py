"""Redis key constants + small read/write helpers.

Schema (rag-stack-compatible so existing tools observe the same shape):

  crawler:queue     LIST   pending URL jobs (orjson-serialised dicts)
  crawler:seen      SET    sha1(url) of successful crawls; trimmed at SEEN_CAP
  crawler:feeds     HASH   feed_id -> feed config json
  crawler:jobs      ZSET   recent crawl outcomes, scored by timestamp ms

The helpers are pure-redis (no fetch / browser concerns). They are
shared between the dispatcher worker (``spider/queue.py``) and the HTTP
routes (``spider/routes.py``) so both surfaces produce the same job
records.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Any

import orjson

# ─── env knobs ──────────────────────────────────────────────────────
SEEN_CAP = int(os.environ.get("CRAWLER_SEEN_CAP", "250000"))
SEEN_TRIM = int(os.environ.get("CRAWLER_SEEN_TRIM", "1000"))
QUEUE_CAP = int(os.environ.get("CRAWLER_QUEUE_CAP", "10000"))
JOBS_KEEP = int(os.environ.get("JOBS_KEEP", "500"))

# ─── key constants ──────────────────────────────────────────────────
K_QUEUE = "crawler:queue"
K_SEEN = "crawler:seen"
K_FEEDS = "crawler:feeds"
K_JOBS = "crawler:jobs"


@lru_cache(maxsize=4096)
def url_hash(url: str) -> str:
    """sha1 of the URL bytes — the canonical seen-set member."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


# ─── queue ──────────────────────────────────────────────────────────
async def enqueue(r: Any, url: str, *, source: str = "manual") -> None:
    """LPUSH a JSON job onto crawler:queue, trim to QUEUE_CAP."""
    await r.lpush(K_QUEUE, orjson.dumps({"url": url, "source": source}))
    if QUEUE_CAP and await r.llen(K_QUEUE) > QUEUE_CAP:
        await r.ltrim(K_QUEUE, 0, QUEUE_CAP - 1)


async def queue_depth(r: Any) -> int:
    return int(await r.llen(K_QUEUE))


# ─── seen-set ───────────────────────────────────────────────────────
async def mark_seen(r: Any, url: str) -> None:
    """SADD the URL's sha1 with a rolling cap so the SET doesn't grow
    without bound. Trims a random sample of size SEEN_TRIM before SADD
    so the just-added hash can't be immediately evicted.
    """
    h = url_hash(url)
    if SEEN_CAP and await r.scard(K_SEEN) >= SEEN_CAP:
        victims = await r.srandmember(K_SEEN, SEEN_TRIM)
        if victims:
            await r.srem(K_SEEN, *victims)
    await r.sadd(K_SEEN, h)


async def is_seen(r: Any, url: str) -> bool:
    return bool(await r.sismember(K_SEEN, url_hash(url)))


async def seen_count(r: Any) -> int:
    return int(await r.scard(K_SEEN))


# ─── jobs ZSET ──────────────────────────────────────────────────────
async def record_job(r: Any, job: dict[str, Any]) -> None:
    """ZADD a job record to crawler:jobs keyed by job[``ts``]; trim to
    the most recent JOBS_KEEP entries.

    ``job`` must include ``id``, ``url``, ``status``, ``ms``, ``ts``.
    Other keys are stored verbatim.
    """
    score = float(job.get("ts", 0))
    await r.zadd(K_JOBS, {orjson.dumps(job): score})
    n = await r.zcard(K_JOBS)
    if n > JOBS_KEEP:
        await r.zremrangebyrank(K_JOBS, 0, n - JOBS_KEEP - 1)


async def recent_jobs(r: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return the ``limit`` most recent job records, newest first."""
    limit = max(1, min(500, int(limit)))
    raw = await r.zrevrange(K_JOBS, 0, limit - 1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(orjson.loads(item))
        except orjson.JSONDecodeError:
            continue
    return out


async def jobs_count(r: Any) -> int:
    return int(await r.zcard(K_JOBS))


# ─── feeds ──────────────────────────────────────────────────────────
async def feeds_count(r: Any) -> int:
    return int(await r.hlen(K_FEEDS))


__all__ = [
    "JOBS_KEEP", "K_FEEDS", "K_JOBS", "K_QUEUE", "K_SEEN",
    "QUEUE_CAP", "SEEN_CAP", "SEEN_TRIM",
    "enqueue", "feeds_count", "is_seen", "jobs_count", "mark_seen",
    "queue_depth", "recent_jobs", "record_job", "seen_count",
    "url_hash",
]
