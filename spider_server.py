"""aiohttp HTTP server — rag-stack-compatible :11700 surface.

PR 1 ships ``GET /health`` + ``POST /fetch/sync``. PR 2 adds the async
``POST /fetch`` queue surface, ``/feeds*``, and ``/jobs``.

The HTTP surface and the AgentField surface (main.py) run as two
services in compose / two containers in k3s, sharing the same image.
Both call into the shared ``spider/`` module so the two surfaces
never drift.
"""
from __future__ import annotations

import logging
import os

import aiohttp
from aiohttp import web

from spider.browser import (
    BROWSER_MAX_PAGES, CloakBrowserPool,
)
from spider.fetch import FetchDeps
from spider.politeness import HostBucket, RobotsCache
from spider.routes import register_routes

logger = logging.getLogger("spider-http")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


async def _on_startup(app: web.Application) -> None:
    """Build the long-lived deps shared across requests.

    The CloakBrowser pool is lazy — the browser process isn't started
    until the first ``/fetch/sync`` lease, so /health on a fresh pod
    answers without paying the Chromium boot cost.
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
    logger.info("spider-http deps ready")


async def _on_cleanup(app: web.Application) -> None:
    """Release the deps in reverse-construction order."""
    pool: CloakBrowserPool | None = app.get("browser")
    if pool is not None:
        await pool.shutdown()
    http: aiohttp.ClientSession | None = app.get("http")
    if http is not None:
        await http.close()
    logger.info("spider-http deps released")


def build_app() -> web.Application:
    """Construct the aiohttp app with routes + lifecycle hooks.

    Kept as a free function (no class) so tests can call it without
    instantiating a server. The startup hook does the real wiring and
    is only invoked when ``web.run_app`` boots the app — meaning
    ``build_app()`` is safe to call from unit tests without any
    Redis / browser / network side effects.
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
