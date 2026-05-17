"""aiohttp route handlers.

PR 1 shipped ``GET /health`` + ``POST /fetch/sync``.
PR 2 adds the async surfaces:

  POST   /fetch                {url}                          → enqueue
  POST   /feeds                {url, interval_s?, name?}      → register
  GET    /feeds                                               → list
  GET    /feeds/{id}                                          → get one
  DELETE /feeds/{id}                                          → remove
  POST   /feeds/{id}/poll                                     → force-poll
  GET    /jobs?limit=50                                       → recent

``/health`` is expanded with queue / feeds / seen / jobs counts.

The handlers stay thin — they delegate to ``spider/{state,feeds,queue}``
so the AgentField surface (PR 3) can reuse the same helpers without
going through HTTP.
"""
from __future__ import annotations

import os
from typing import Any

import orjson
from aiohttp import web

from . import feeds as feeds_mod
from .fetch import FetchDeps
from .log import log
from .queue import run_and_record
from .security import is_safe_external_url
from .state import (
    enqueue, feeds_count, jobs_count, queue_depth, recent_jobs, seen_count,
)


_CONTENT_JSON = "application/json"


def _json(obj: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=orjson.dumps(obj),
        status=status,
        content_type=_CONTENT_JSON,
    )


async def _read_json(request: web.Request) -> tuple[dict | None, str | None]:
    """Parse the request body as JSON; return ``(payload, error)``.

    Used by every POST handler so the error shape is consistent.
    """
    try:
        body = await request.read()
        if not body:
            return {}, None
        return orjson.loads(body), None
    except orjson.JSONDecodeError:
        return None, "invalid json body"
    except Exception:
        return None, "could not read body"


# ─── health ─────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    """Liveness probe + summary counts.

    Reports the browser pool state plus Redis-backed counts: queue
    depth, registered feeds, seen-set size, recorded jobs. Falls back
    gracefully when Redis isn't yet connected (e.g. during startup).
    """
    pool = request.app.get("browser")
    browser_state = "absent"
    if pool is not None:
        browser_state = (
            "open" if getattr(pool, "_browser", None) is not None else "closed"
        )

    redis = request.app.get("redis")
    counts: dict[str, Any] = {}
    if redis is not None:
        try:
            counts = {
                "queue": await queue_depth(redis),
                "feeds": await feeds_count(redis),
                "seen": await seen_count(redis),
                "jobs": await jobs_count(redis),
            }
        except Exception as e:  # pragma: no cover
            counts = {"error": f"{type(e).__name__}: {e}"}

    return _json({
        "ok": True,
        "service": "spider-http",
        "browser": browser_state,
        "fetch_timeout_ms": int(os.environ.get("FETCH_TIMEOUT_MS", "30000")),
        **counts,
    })


# ─── /fetch + /fetch/sync ───────────────────────────────────────────
async def fetch_async(request: web.Request) -> web.Response:
    """``POST /fetch`` — enqueue a URL, return immediately.

    Response (200)::
        {"queued": true, "url": <url>}

    400 on missing/invalid url or layer-1 SSRF gate failure.
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)
    url = (payload or {}).get("url", "")
    if not isinstance(url, str) or not url:
        return _json({"error": "url required"}, status=400)
    if not is_safe_external_url(url):
        log("fetch.blocked", url=url)
        return _json({"error": "url failed SSRF gate", "url": url}, status=400)
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    await enqueue(redis, url, source="api")
    return _json({"queued": True, "url": url})


async def fetch_sync(request: web.Request) -> web.Response:
    """``POST /fetch/sync`` — fetch inline, return text.

    Status codes:
      200 — ok
      204 — empty content (< 100 chars)
      400 — invalid body / layer-1 SSRF / redirect-SSRF
      403 — robots disallowed
      502 — fetch / extract error

    Records the outcome to ``crawler:jobs`` when Redis is available,
    so observability via ``GET /jobs`` is uniform across the sync and
    async paths.
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)
    url = (payload or {}).get("url", "")
    if not isinstance(url, str) or not url:
        return _json({"error": "url required"}, status=400)
    if not is_safe_external_url(url):
        log("ssrf", url=url, gate="layer1")
        return _json({"error": "url failed SSRF gate", "url": url}, status=400)

    deps: FetchDeps = request.app["deps"]
    redis = request.app.get("redis")
    result = await run_and_record(deps, redis, url, source="api-sync")

    status = result.get("status")
    if status == "ok":
        return _json(result, status=200)
    if status == "empty":
        return _json(result, status=204)
    if status == "blocked-by-robots":
        return _json(result, status=403)
    if status == "blocked-by-ssrf-gate":
        return _json(result, status=400)
    return _json(result, status=502)


# ─── /feeds ─────────────────────────────────────────────────────────
async def feeds_add(request: web.Request) -> web.Response:
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)
    url = ((payload or {}).get("url") or "").strip()
    if not url or not isinstance(url, str):
        return _json({"error": "url required"}, status=400)
    if not is_safe_external_url(url):
        log("feed.blocked", url=url)
        return _json({"error": "url failed SSRF gate"}, status=400)
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    feed = await feeds_mod.feed_add(
        redis,
        url=url,
        name=(payload or {}).get("name"),
        interval_s=(payload or {}).get("interval_s"),
    )
    return _json(feed)


async def feeds_list(request: web.Request) -> web.Response:
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    return _json({"feeds": await feeds_mod.feed_list(redis)})


async def feeds_get(request: web.Request) -> web.Response:
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    feed_id = request.match_info["id"]
    feed = await feeds_mod.feed_get(redis, feed_id)
    if feed is None:
        return _json({"error": "not found"}, status=404)
    return _json(feed)


async def feeds_del(request: web.Request) -> web.Response:
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    feed_id = request.match_info["id"]
    deleted = await feeds_mod.feed_del(redis, feed_id)
    if not deleted:
        return _json({"error": "not found"}, status=404)
    return _json({"ok": True})


async def feeds_poll(request: web.Request) -> web.Response:
    """``POST /feeds/{id}/poll`` — force-poll one feed."""
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)
    feed_id = request.match_info["id"]
    feed = await feeds_mod.feed_get(redis, feed_id)
    if feed is None:
        return _json({"error": "not found"}, status=404)
    deps: FetchDeps = request.app["deps"]
    result = await feeds_mod.poll_feed(deps.http, redis, feed_id, feed)
    return _json({"feed_id": feed_id, **result})


# ─── /jobs ──────────────────────────────────────────────────────────
async def jobs(request: web.Request) -> web.Response:
    redis = request.app.get("redis")
    if redis is None:
        return _json({"jobs": []})
    limit = int(request.query.get("limit", "50") or "50")
    return _json({"jobs": await recent_jobs(redis, limit=limit)})


# ─── registration ───────────────────────────────────────────────────
def register_routes(app: web.Application) -> None:
    """Wire all v1 routes into ``app``. Idempotent across distinct apps."""
    app.router.add_get("/health", health)
    app.router.add_post("/fetch", fetch_async)
    app.router.add_post("/fetch/sync", fetch_sync)
    app.router.add_post("/feeds", feeds_add)
    app.router.add_get("/feeds", feeds_list)
    app.router.add_get("/feeds/{id}", feeds_get)
    app.router.add_delete("/feeds/{id}", feeds_del)
    app.router.add_post("/feeds/{id}/poll", feeds_poll)
    app.router.add_get("/jobs", jobs)


__all__ = [
    "feeds_add", "feeds_del", "feeds_get", "feeds_list", "feeds_poll",
    "fetch_async", "fetch_sync",
    "health", "jobs",
    "register_routes",
]
