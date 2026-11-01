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

On startup we connect to Redis (via ``REDIS_URL``) and spawn three
background tasks:

  - **dispatcher** drains ``crawler:queue`` and fan-outs to bounded
    workers.
  - **crawl_dispatcher** scans ``crawler:crawl:*:state`` and runs
    ``crawl_iter`` for active crawl jobs.
  - **poller** wakes when the next-due feed becomes due and re-polls it.

Both are cancel-safe and unwind cleanly when ``_on_cleanup`` fires.

This module is the production entry point. Tests construct their own
app via ``spider.routes.register_routes`` so they don't run the
startup hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable

import aiohttp
import redis.asyncio as redis_asyncio
from aiohttp import web

from spider import storage as pg_storage
from spider.browser import BROWSER_MAX_PAGES, CloakBrowserPool
from spider.browsers import env_backend, make_pool, register_primary, shutdown_all
from spider.crawl import crawl_dispatcher
from spider.feeds import poller
from spider.fetch import FetchDeps
from spider.politeness import HostBucket, RobotsCache
from spider.profiles import ProfileStore
from spider.queue import dispatcher
from spider.routes import register_routes
from spider.sessions import SessionManager

logger = logging.getLogger("spider-http")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning("%s=%r is invalid; using default=%s", name, raw, default)
        return default
    if min_value is not None and value < min_value:
        logger.warning("%s=%s is below min=%s; using default=%s", name, value, min_value, default)
        return default
    return value


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s=%r is invalid; using default=%s", name, raw, default)
        return default
    if min_value is not None and value < min_value:
        logger.warning("%s=%s is below min=%s; using default=%s", name, value, min_value, default)
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    logger.warning("%s=%r is invalid boolean; using default=%s", name, raw, int(default))
    return default


HTTP_KEEPALIVE_TIMEOUT_S = _env_float("SPIDER_HTTP_KEEPALIVE_TIMEOUT_S", 45.0, min_value=0.1)
HTTP_CONN_LIMIT = _env_int("SPIDER_HTTP_CONN_LIMIT", 200, min_value=1)
HTTP_CONN_LIMIT_PER_HOST = _env_int("SPIDER_HTTP_CONN_LIMIT_PER_HOST", 50, min_value=1)
HTTP_DNS_CACHE_TTL_S = _env_int("SPIDER_HTTP_DNS_CACHE_TTL_S", 300, min_value=0)
HTTP_ENABLE_CLEANUP_CLOSED = _env_bool("SPIDER_HTTP_ENABLE_CLEANUP_CLOSED", True)


def _build_http_session(
    *,
    trace_configs: list[aiohttp.TraceConfig] | None = None,
) -> aiohttp.ClientSession:
    """Create the shared outbound aiohttp session with explicit reuse knobs.

    This session is used by the redirect walker + robots cache checks.
    ``keepalive_timeout`` / ``force_close=False`` make connection reuse
    explicit across sequential fetches and avoid regressions if aiohttp
    defaults change in future releases.
    """
    connector = aiohttp.TCPConnector(
        keepalive_timeout=HTTP_KEEPALIVE_TIMEOUT_S,
        ttl_dns_cache=HTTP_DNS_CACHE_TTL_S,
        limit=HTTP_CONN_LIMIT,
        limit_per_host=HTTP_CONN_LIMIT_PER_HOST,
        force_close=False,
        enable_cleanup_closed=HTTP_ENABLE_CLEANUP_CLOSED,
    )
    return aiohttp.ClientSession(
        connector=connector,
        trace_configs=trace_configs or [],
    )


async def _on_startup(app: web.Application) -> None:
    """Build long-lived deps + spawn background tasks.

    CloakBrowser is opened lazily on the first lease (``_open`` only
    runs inside ``CloakBrowserPool.lease()``), so the Chromium binary
    isn't fetched until the first ``/fetch/sync`` arrives.
    """
    http = _build_http_session()
    # Choose the primary browser backend from env. Defaults to cloak.
    # The pool's _open() is lazy — Chromium / CDP isn't initialised
    # until the first lease, so this is cheap even if we never fetch.
    backend = env_backend()
    pool = make_pool(backend, max_pages=BROWSER_MAX_PAGES)
    register_primary(backend, pool)
    robots = RobotsCache(http)
    bucket = HostBucket()
    sessions = SessionManager()
    deps = FetchDeps(
        http=http,
        pool=pool,
        robots=robots,
        bucket=bucket,
        sessions=sessions,
    )
    app["http"] = http
    app["browser"] = pool
    app["robots"] = robots
    app["bucket"] = bucket
    app["sessions"] = sessions
    app["deps"] = deps
    logger.info("spider-http browser backend=%s", backend)
    logger.info(
        "spider-http http session keepalive=%ss dns_ttl=%ss limits=%s/%s",
        HTTP_KEEPALIVE_TIMEOUT_S,
        HTTP_DNS_CACHE_TTL_S,
        HTTP_CONN_LIMIT,
        HTTP_CONN_LIMIT_PER_HOST,
    )

    # Optional postgres backend for profiles + (future) cross-restart
    # session recovery. When POSTGRES_URL isn't set, ProfileStore
    # becomes a no-op and the /profiles routes return 503.
    pg_pool = await pg_storage.connect()
    app["pg_pool"] = pg_pool
    app["profiles"] = ProfileStore(pg_pool)
    if pg_pool is not None:
        logger.info("spider-http profile store ready (postgres)")
    else:
        logger.info("spider-http profiles disabled (no POSTGRES_URL)")

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis_asyncio.from_url(redis_url, decode_responses=False)
    app["redis"] = r
    logger.info("spider-http connected redis=%s", redis_url)

    # Spawn background tasks. They both BRPOP / sleep on Redis, so a
    # broken Redis surfaces as task failures the next time they wake
    # rather than during startup.
    app["dispatcher_task"] = asyncio.create_task(dispatcher(deps, r))
    app["crawl_dispatcher_task"] = asyncio.create_task(crawl_dispatcher(deps, r))
    app["poller_task"] = asyncio.create_task(poller(http, r))
    logger.info("spider-http background tasks started")


async def _cleanup_step(
    name: str,
    coro: Awaitable[object],
    timeout: float = 5.0,
) -> bool:
    """Run one cleanup step with a hard timeout. Logs success or failure.

    Returns True if step completed cleanly within `timeout` seconds,
    False if it timed out or raised. Never re-raises — cleanup must
    proceed even if one step fails so we don't leak the rest.
    """
    import time

    started = time.perf_counter()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("cleanup.%s ok elapsed_ms=%.0f", name, elapsed_ms)
        return True
    except TimeoutError:
        logger.warning("cleanup.%s TIMEOUT after %.1fs", name, timeout)
        return False
    except Exception as e:
        logger.warning("cleanup.%s error %s: %s", name, type(e).__name__, e)
        return False


async def _on_cleanup(app: web.Application) -> None:
    """Cancel background tasks, then release deps in reverse order.

    Each step is wrapped in a hard timeout so a hung browser child /
    flaky redis connection can't block past docker's stop_grace_period
    (set to 30s in docker-compose.yml; we target <20s here so SIGKILL
    has a buffer). Steps that fail or time out are logged but don't
    prevent later steps from running.
    """
    import time

    cleanup_started = time.perf_counter()
    logger.info("spider-http cleanup begin")

    # 1. Cancel background tasks (cheap, ~instant)
    for key in ("dispatcher_task", "crawl_dispatcher_task", "poller_task"):
        t: asyncio.Task[None] | None = app.get(key)
        if t is None or t.done():
            continue
        t.cancel()

        async def _await_cancel(task: asyncio.Task[None] = t) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        await _cleanup_step(f"cancel.{key}", _await_cancel(), timeout=3.0)

    # 2. Sessions — close any open BrowserContexts before the pool
    # they live on goes away
    sessions: SessionManager | None = app.get("sessions")
    if sessions is not None:
        await _cleanup_step(
            "sessions.close_all",
            sessions.close_all(),
            timeout=5.0,
        )

    # 3. Browser pool — slowest step (Chromium child may need 2-5s to die)
    # Plus close any alternate pools the registry spun up.
    pool: CloakBrowserPool | None = app.get("browser")
    if pool is not None:
        await _cleanup_step("browser.shutdown", pool.shutdown(), timeout=10.0)
    await _cleanup_step("browsers.shutdown_all", shutdown_all(), timeout=5.0)

    # 4. Redis — should be near-instant; longer timeout is paranoia
    r = app.get("redis")
    if r is not None:
        await _cleanup_step("redis.aclose", r.aclose(), timeout=3.0)

    # 5. aiohttp ClientSession — close all keep-alive conns
    http: aiohttp.ClientSession | None = app.get("http")
    if http is not None:
        await _cleanup_step("http.close", http.close(), timeout=5.0)

    # 6. Postgres pool
    pg_pool = app.get("pg_pool")
    if pg_pool is not None:
        await _cleanup_step(
            "pg_pool.close",
            pg_storage.close(pg_pool),
            timeout=5.0,
        )

    total_ms = (time.perf_counter() - cleanup_started) * 1000
    logger.info("spider-http cleanup done total_ms=%.0f", total_ms)


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
