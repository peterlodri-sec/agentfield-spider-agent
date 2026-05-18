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
from .crawl import crawl_iter, crawl_start, crawl_status
from .fetch import FetchDeps
from .log import log
from .queue import run_and_record
from .security import is_safe_external_url
from .sitemap import sitemap_parse
from .sinks.chroma import should_sink, sink_fetch_outcome
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

    Request body::

        {
          "url": "https://...",
          "use_browser": false,   // optional; force the browser path
                                  // (otherwise URL kind decides)
          "proxy": "..."          // optional per-call proxy; overrides
                                  // SPIDER_PROXY_URL for HTTP fetchers
                                  // only (browser pool is shared)
        }

    Status codes:
      200 — ok
      204 — empty content (< 100 chars)
      400 — invalid body / layer-1 SSRF / redirect-SSRF
      403 — robots disallowed
      502 — fetch / extract error

    URL routing (when ``use_browser`` is unset / false):
      github.com/{owner}/{repo}       -> GitHub REST API
      github.com/.../issues/N         -> GitHub REST API
      news.ycombinator.com/item?id=N  -> HN Firebase API
      reddit.com/...                  -> Reddit .json
      anything else                   -> CloakBrowser-rendered (the
                                          original v1 path)

    Records the outcome to ``crawler:jobs`` when Redis is available,
    so observability via ``GET /jobs`` is uniform across sync, async,
    and specialized-fetcher paths.
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

    use_browser = bool((payload or {}).get("use_browser", False))
    proxy_override = (payload or {}).get("proxy")
    if proxy_override is not None and not isinstance(proxy_override, str):
        return _json(
            {"error": "proxy must be a string if provided"}, status=400,
        )

    deps: FetchDeps = request.app["deps"]
    redis = request.app.get("redis")
    result = await run_and_record(
        deps, redis, url,
        source="api-sync",
        use_browser=use_browser,
        proxy=proxy_override,
    )

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


# ─── /crawl (recursive crawl with depth limit) ───────────────────────
async def crawl(request: web.Request) -> web.Response:
    """``POST /crawl`` — start a recursive crawl job.

    Request body: {url, max_depth?, max_pages?, same_domain_only?, timeout_seconds?}
    Response (200): {job_id, status, seed_url, pages_fetched, pages_queued, ...}
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)

    url = (payload or {}).get("url", "").strip()
    if not url or not isinstance(url, str):
        return _json({"error": "url required"}, status=400)

    if not is_safe_external_url(url):
        log("crawl.blocked", url=url)
        return _json({"error": "url failed SSRF gate", "url": url}, status=400)

    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)

    max_depth = int((payload or {}).get("max_depth", 2) or 2)
    max_pages = int((payload or {}).get("max_pages", 50) or 50)
    same_domain_only = (payload or {}).get("same_domain_only", True)
    if not isinstance(same_domain_only, bool):
        same_domain_only = str(same_domain_only).lower() in ("true", "1", "yes")
    timeout_seconds = int((payload or {}).get("timeout_seconds", 600) or 600)

    job_id = await crawl_start(
        redis,
        url,
        max_depth=max_depth,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        timeout_seconds=timeout_seconds,
    )

    status = await crawl_status(redis, job_id)
    return _json({"job_id": job_id, **status})


async def crawl_status_route(request: web.Request) -> web.Response:
    """``GET /crawl/{job_id}`` — get crawl job status."""
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)

    job_id = request.match_info["job_id"]
    status = await crawl_status(redis, job_id)
    if status.get("status") == "not_found":
        return _json(status, status=404)
    return _json(status)


async def crawl_results(request: web.Request) -> web.Response:
    """``GET /crawl/{job_id}/results`` — get crawl results (list of fetched pages)."""
    redis = request.app.get("redis")
    if redis is None:
        return _json({"error": "redis not configured"}, status=503)

    job_id = request.match_info["job_id"]
    results_key = f"crawler:crawl:{job_id}:results"

    # Check if job exists
    state_key = f"crawler:crawl:{job_id}:state"
    state = await redis.hgetall(state_key)
    if not state:
        return _json({"error": "not found"}, status=404)

    # Fetch results
    results_raw = await redis.lrange(results_key, 0, -1)
    results = []
    for item in results_raw:
        try:
            results.append(orjson.loads(item))
        except Exception:
            pass

    return _json({"job_id": job_id, "count": len(results), "results": results})


# ─── /sitemap (parse sitemap.xml) ────────────────────────────────────
async def sitemap_handler(request: web.Request) -> web.Response:
    """``POST /sitemap/parse`` — parse a sitemap.xml and optionally enqueue URLs.

    Request body: {url, queue?, max_urls?}
    Response (200): {url, urls: [...], count, queued, errors?}
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)

    url = (payload or {}).get("url", "").strip()
    if not url or not isinstance(url, str):
        return _json({"error": "url required"}, status=400)

    if not is_safe_external_url(url):
        log("sitemap.blocked", url=url)
        return _json({"error": "url failed SSRF gate"}, status=400)

    http = request.app.get("http")
    if http is None:
        return _json({"error": "http session not configured"}, status=503)

    redis = request.app.get("redis") if (payload or {}).get("queue") else None

    max_urls = int((payload or {}).get("max_urls", 1000) or 1000)
    queue_flag = (payload or {}).get("queue", False)

    result = await sitemap_parse(
        http,
        url,
        max_urls=max_urls,
        queue=queue_flag,
        redis=redis,
    )
    return _json(result)


# ─── /screenshot (browser screenshot) ────────────────────────────────
async def screenshot(request: web.Request) -> web.Response:
    """``POST /screenshot`` — take a browser screenshot of a URL.

    Request body: {url, full_page?}
    Response (200): {ok, png_b64, ms} or error
    Status codes: 200 ok, 400 SSRF, 502 browser error
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)

    url = (payload or {}).get("url", "").strip()
    if not url or not isinstance(url, str):
        return _json({"error": "url required"}, status=400)

    if not is_safe_external_url(url):
        log("screenshot.blocked", url=url)
        return _json({"error": "url failed SSRF gate"}, status=400)

    pool = request.app.get("browser")
    if pool is None:
        return _json({"error": "browser pool not configured"}, status=503)

    full_page = (payload or {}).get("full_page", True)
    if not isinstance(full_page, bool):
        full_page = str(full_page).lower() in ("true", "1", "yes")

    import time
    t0 = time.perf_counter()

    try:
        async with pool.lease() as page:
            await page.goto(url, wait_until="networkidle")
            png_bytes = await page.screenshot(full_page=full_page)
            import base64
            png_b64 = base64.b64encode(png_bytes).decode("utf-8")

            ms = int((time.perf_counter() - t0) * 1000)
            return _json({"ok": True, "png_b64": png_b64, "ms": ms})

    except Exception as e:
        log("screenshot.error", url=url, error=f"{type(e).__name__}: {e}")
        return _json({"error": f"{type(e).__name__}: {e}", "url": url}, status=502)


# ─── /extract (CSS selector extraction) ──────────────────────────────
async def extract(request: web.Request) -> web.Response:
    """``POST /extract`` — extract content via CSS selector.

    Request body: {url, selector, attr?}
    If attr is set, returns attribute values; else text content.
    Response (200): {ok, matches: [...], ms}
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)

    url = (payload or {}).get("url", "").strip()
    selector = (payload or {}).get("selector", "").strip()

    if not url or not selector:
        return _json({"error": "url and selector required"}, status=400)

    if not is_safe_external_url(url):
        log("extract.blocked", url=url)
        return _json({"error": "url failed SSRF gate"}, status=400)

    pool = request.app.get("browser")
    if pool is None:
        return _json({"error": "browser pool not configured"}, status=503)

    attr = (payload or {}).get("attr")

    import time
    t0 = time.perf_counter()

    try:
        async with pool.lease() as page:
            await page.goto(url, wait_until="networkidle")

            if attr:
                # Get attribute values
                results = await page.evaluate(
                    f"""() => {{
                        const elems = document.querySelectorAll('{selector}');
                        return Array.from(elems).map(e => e.getAttribute('{attr}'));
                    }}"""
                )
            else:
                # Get text content
                results = await page.evaluate(
                    f"""() => {{
                        const elems = document.querySelectorAll('{selector}');
                        return Array.from(elems).map(e => e.textContent);
                    }}"""
                )

            ms = int((time.perf_counter() - t0) * 1000)
            return _json({"ok": True, "matches": results or [], "ms": ms})

    except Exception as e:
        log("extract.error", url=url, selector=selector, error=f"{type(e).__name__}: {e}")
        return _json({"error": f"{type(e).__name__}: {e}", "url": url}, status=502)


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
    app.router.add_post("/crawl", crawl)
    app.router.add_get("/crawl/{job_id}", crawl_status_route)
    app.router.add_get("/crawl/{job_id}/results", crawl_results)
    app.router.add_post("/sitemap/parse", sitemap_handler)
    app.router.add_post("/screenshot", screenshot)
    app.router.add_post("/extract", extract)


__all__ = [
    "crawl", "crawl_results", "crawl_status_route",
    "extract",
    "feeds_add", "feeds_del", "feeds_get", "feeds_list", "feeds_poll",
    "fetch_async", "fetch_sync",
    "health", "jobs",
    "register_routes",
    "screenshot",
    "sitemap_handler",
]
