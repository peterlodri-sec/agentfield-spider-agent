"""Async fetch worker — Redis BRPOP-blocked dispatcher.

Single coro pops job descriptors off ``crawler:queue``, spawns a
bounded task per job. Each worker calls ``fetch_one`` then records the
outcome (mark_seen on ok, record_job always).

The HTTP ``POST /fetch/sync`` route shares the same record-and-mark
helper (``run_and_record``) so both surfaces produce identical
``crawler:jobs`` entries.

Ported from rag-stack/scripts/crawler.py lines 731-789 with the topic
quality-gate plumbing removed (jobs only carry ``status`` /
``error`` / ``score`` when set by the fetch pipeline itself).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import orjson

from .dispatch import dispatch_fetch
from .fetch import FetchDeps
from .log import log
from .sinks.chroma import should_sink, sink_fetch_outcome
from .state import K_QUEUE, enqueue, mark_seen, record_job

# ─── env knobs ──────────────────────────────────────────────────────
CRAWL_CONCURRENCY = int(os.environ.get("CRAWL_CONCURRENCY", "3"))


async def run_and_record(
    deps: FetchDeps,
    redis: Any,
    url: str,
    *,
    source: str = "manual",
    use_browser: bool = False,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Call ``dispatch_fetch`` and record the outcome.

    The dispatcher routes by URL kind (github / hn / reddit /
    generic). ``use_browser=True`` forces the browser-rendered path
    regardless of URL kind. ``proxy`` overrides ``SPIDER_PROXY_URL``
    for the specialized HTTP fetchers (browser path uses env proxy).

    Always returns the dispatcher's flat-dict, with ``source``
    added for the jobs ZSET. Side effects on Redis:
      - successful fetch (status="ok") -> SADD ``crawler:seen``
      - every outcome -> ZADD ``crawler:jobs``

    If the chromadb sink is enabled and the fetch is ok, also posts
    to the sink (silently skips on error).

    If ``redis`` is None the recording is skipped (useful for
    standalone tests of the fetch pipeline alone).
    """
    result = await dispatch_fetch(
        deps, url, use_browser=use_browser, proxy=proxy,
    )
    result["source"] = source
    if redis is not None:
        if result.get("status") == "ok":
            await mark_seen(redis, url)
        await record_job(redis, result)

    # Sink to chromadb if configured and status is ok
    if should_sink() and result.get("status") == "ok":
        await sink_fetch_outcome(deps.http, result)

    return result


async def _run_one(
    deps: FetchDeps,
    redis: Any,
    raw: bytes,
    sem: asyncio.Semaphore,
) -> None:
    """One worker iteration: decode raw job, fetch + record, release sem."""
    try:
        try:
            job = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        url = job.get("url")
        source = job.get("source", "manual")
        if not isinstance(url, str) or not url:
            return
        try:
            await run_and_record(deps, redis, url, source=source)
        except Exception as e:  # pragma: no cover
            log("worker", state="error", msg=f"{type(e).__name__}: {e}")
    finally:
        sem.release()


async def dispatcher(deps: FetchDeps, redis: Any) -> None:
    """BRPOP-blocked dispatcher. One asyncio task, bounded fan-out.

    Cancel-safe — on ``CancelledError`` it cancels in-flight workers
    and re-raises so the lifecycle hook unwinds cleanly.
    """
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)
    pending: set[asyncio.Task] = set()
    log("dispatcher", state="ready", concurrency=CRAWL_CONCURRENCY)
    try:
        while True:
            popped = await redis.brpop([K_QUEUE], timeout=0)
            if popped is None:
                continue
            _key, raw = popped
            await sem.acquire()
            t = asyncio.create_task(_run_one(deps, redis, raw, sem))
            pending.add(t)
            t.add_done_callback(pending.discard)
    except asyncio.CancelledError:
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        log("dispatcher", state="stopped")
        raise


__all__ = [
    "CRAWL_CONCURRENCY",
    "dispatcher",
    "enqueue",
    "run_and_record",
]
