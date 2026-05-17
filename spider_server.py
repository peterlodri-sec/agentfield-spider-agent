"""aiohttp HTTP server — rag-stack-compatible :11700 surface.

Runs the full v1 surface:

  GET    /health
  POST   /fetch            enqueue
  POST   /fetch/sync       inline fetch
  POST   /feeds            register
  GET    /feeds            list
  GET    /feeds/{id}       get one
  DELETE /feeds/{id}       remove
  POST   /feeds/{id}/poll  force-poll
  GET    /jobs?limit=50    recent

On startup we connect to Redis (via ``REDIS_URL``) and spawn two
background tasks:

  - **dispatcher** drains ``crawler:queue`` and fan-outs to bounded
    workers.
  - **poller** wakes when the next-due feed becomes due and re-polls it.

Both are cancel-safe and unwind cleanly when ``_on_cleanup`` fires.

This module is the production entry point. Tests construct their own
app via ``spider.routes.register_routes`` so they don't run the
startup hook.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import redis.asyncio as redis_asyncio
from aiohttp import web

from spider.browser import BROWSER_MAX_PAGES, CloakBrowserPool
from spider.feeds import poller
from spider.fetch import FetchDeps
from spider.politeness import HostBucket, RobotsCache
from spider.queue import dispatcher
from spider.routes import register_routes

logger = logging.getLogger("spider-http")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


async def _on_startup(app: web.Application) -> None:
    """Build long-lived deps + spawn background tasks.

    CloakBrowser is opened lazily on the first lease (``_open`` only
    runs inside ``CloakBrowserPool.lease()``), so the Chromium binary
    isn't fetched until the first ``/fetch/sync`` arrives.
    """
    http = aiohttp.ClientSession()
    pool = CloakBrowserPool(max_pages=BROWSER_MAX_PAGES)
    robots = RobotsCache(http)
    bucket = HostBucket()
    deps = FetchDeps(http=http, pool=pool, robots=robots, bucket=bucket)
    app["http"] = http
    app["browser"] = pool
    app["robots"] = robots
    app["bucket"] = bucket
    app["deps"] = deps

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis_asyncio.from_url(redis_url, decode_responses=False)
    app["redis"] = r
    logger.info("spider-http connected redis=%s", redis_url)

    # Spawn background tasks. They both BRPOP / sleep on Redis, so a
    # broken Redis surfaces as task failures the next time they wake
    # rather than during startup.
    app["dispatcher_task"] = asyncio.create_task(dispatcher(deps, r))
    app["poller_task"] = asyncio.create_task(poller(http, r))
    logger.info("spider-http background tasks started")


async def _on_cleanup(app: web.Application) -> None:
    """Cancel background tasks, then release deps in reverse order."""
    for key in ("dispatcher_task", "poller_task"):
        t: asyncio.Task | None = app.get(key)
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    pool: CloakBrowserPool | None = app.get("browser")
    if pool is not None:
        await pool.shutdown()
    r = app.get("redis")
    if r is not None:
        await r.aclose()
    http: aiohttp.ClientSession | None = app.get("http")
    if http is not None:
        await http.close()
    logger.info("spider-http deps released")


def build_app() -> web.Application:
    """Construct the aiohttp app with routes + lifecycle hooks.

    Safe to call from unit tests; the startup hook only fires under
    ``web.run_app`` or ``aiohttp.test_utils.TestServer``.
    """
    app = web.Application()
    register_routes(app)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    port = int(os.getenv("CRAWLER_PORT", "11700"))
    logger.info("spider-http starting on :%d", port)
    web.run_app(build_app(), host="0.0.0.0", port=port, access_log=None)
