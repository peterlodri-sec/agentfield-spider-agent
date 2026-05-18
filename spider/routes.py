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

import contextlib
import os
from typing import Any

import orjson
from aiohttp import web
from aiohttp.multipart import BodyPartReader

from . import feeds as feeds_mod
from .crawl import crawl_start, crawl_status
from .fetch import FetchDeps
from .log import log
from .queue import run_and_record
from .security import is_safe_external_url
from .sitemap import sitemap_parse
from .state import (
    enqueue,
    feeds_count,
    jobs_count,
    queue_depth,
    recent_jobs,
    seen_count,
)

_CONTENT_JSON = "application/json"
_OCTET_STREAM = "application/octet-stream"


def _json(obj: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=orjson.dumps(obj),
        status=status,
        content_type=_CONTENT_JSON,
    )


async def _read_json(request: web.Request) -> tuple[dict[str, Any] | None, str | None]:
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
        browser_state = "open" if getattr(pool, "_browser", None) is not None else "closed"

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

    return _json(
        {
            "ok": True,
            "service": "spider-http",
            "browser": browser_state,
            "fetch_timeout_ms": int(os.environ.get("FETCH_TIMEOUT_MS", "30000")),
            **counts,
        }
    )


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
            {"error": "proxy must be a string if provided"},
            status=400,
        )
    browser_override = (payload or {}).get("browser")
    if browser_override is not None and not isinstance(browser_override, str):
        return _json(
            {"error": "browser must be a string if provided"},
            status=400,
        )
    session_id = (payload or {}).get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return _json(
            {"error": "session_id must be a string if provided"},
            status=400,
        )

    deps: FetchDeps = request.app["deps"]
    redis = request.app.get("redis")
    try:
        result = await run_and_record(
            deps,
            redis,
            url,
            source="api-sync",
            use_browser=use_browser,
            proxy=proxy_override,
            browser=browser_override,
            session_id=session_id,
        )
    except KeyError as e:
        # Session not found / expired — surface cleanly rather than 500.
        return _json({"error": str(e), "session_id": session_id}, status=404)

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
        with contextlib.suppress(Exception):
            results.append(orjson.loads(item))

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


# ─── /sessions ──────────────────────────────────────────────────────
async def sessions_create(request: web.Request) -> web.Response:
    """``POST /sessions`` — create a new browser session (BrowserContext).

    Body (all fields optional)::

        {
          "name": "alice",                  // display name; not required
          "metadata": {"user_id": "..."},   // free-form
          "user_agent": "...",              // override default UA
          "viewport": {"width": 1280, "height": 720},
          "backend": "cloak" | "lightpanda"  // default: env-configured
        }

    Returns the session info dict; the ``id`` field is what callers
    pass on subsequent ``/fetch/sync`` requests via ``session_id``.
    """
    payload, err = await _read_json(request)
    if err:
        return _json({"error": err}, status=400)
    body = payload or {}

    sessions = request.app.get("sessions")
    if sessions is None:
        return _json({"error": "session manager not configured"}, status=503)

    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _json({"error": "name must be a string"}, status=400)
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _json({"error": "metadata must be an object"}, status=400)
    user_agent = body.get("user_agent")
    if user_agent is not None and not isinstance(user_agent, str):
        return _json({"error": "user_agent must be a string"}, status=400)
    viewport = body.get("viewport")
    if viewport is not None and (
        not isinstance(viewport, dict) or "width" not in viewport or "height" not in viewport
    ):
        return _json(
            {"error": "viewport must be {width: int, height: int}"},
            status=400,
        )

    # Evasion knobs. ``persona`` names a preset from
    # spider.evasion.identity; ``evasion_rate_per_min`` overrides the
    # default per-session token bucket.
    persona = body.get("persona")
    if persona is not None and not isinstance(persona, str):
        return _json({"error": "persona must be a string"}, status=400)
    if persona is not None:
        from .evasion import list_personas

        if persona not in list_personas():
            return _json(
                {
                    "error": (
                        f"unknown persona {persona!r}. Known: "
                        f"{', '.join(list_personas())}"
                    ),
                },
                status=400,
            )
    evasion_rate_per_min = body.get("evasion_rate_per_min")
    if evasion_rate_per_min is not None and not isinstance(evasion_rate_per_min, int):
        return _json(
            {"error": "evasion_rate_per_min must be an integer"},
            status=400,
        )

    # Backend selection: default to the primary pool, allow override.
    from .browsers import get_or_create_pool, normalize_backend

    backend_kwarg = body.get("backend")
    if backend_kwarg is not None and not isinstance(backend_kwarg, str):
        return _json({"error": "backend must be a string"}, status=400)
    backend = normalize_backend(backend_kwarg)
    deps: FetchDeps = request.app["deps"]
    from .browsers.base import BrowserPool

    pool: BrowserPool
    if backend == _primary_backend_for(deps):
        pool = deps.pool
    else:
        pool = await get_or_create_pool(backend)

    try:
        info = await sessions.create(
            pool=pool,
            name=name,
            metadata=metadata,
            user_agent=user_agent,
            viewport=viewport,
            backend_label=backend,
            persona=persona,
            evasion_rate_per_min=evasion_rate_per_min,
        )
    except Exception as e:
        return _json(
            {"error": f"{type(e).__name__}: {e}"},
            status=502,
        )
    return _json(info.model_dump(), status=200)


async def sessions_list(request: web.Request) -> web.Response:
    """``GET /sessions`` — list active sessions, newest-touched first."""
    sessions = request.app.get("sessions")
    if sessions is None:
        return _json({"sessions": []})
    infos = await sessions.list()
    return _json({"sessions": [i.model_dump() for i in infos]})


async def sessions_get(request: web.Request) -> web.Response:
    """``GET /sessions/{id}`` — get one session's info."""
    sessions = request.app.get("sessions")
    if sessions is None:
        return _json({"error": "session manager not configured"}, status=503)
    sid = request.match_info["id"]
    info = await sessions.get(sid)
    if info is None:
        return _json({"error": "session not found"}, status=404)
    return _json(info.model_dump())


async def sessions_delete(request: web.Request) -> web.Response:
    """``DELETE /sessions/{id}`` — close a session, freeing its context."""
    sessions = request.app.get("sessions")
    if sessions is None:
        return _json({"error": "session manager not configured"}, status=503)
    sid = request.match_info["id"]
    closed = await sessions.close(sid)
    if not closed:
        return _json({"error": "session not found"}, status=404)
    return _json({"ok": True, "session_id": sid})


def _primary_backend_for(deps: FetchDeps) -> str:
    """Return the backend name of the primary pool. Reuses the class-name
    heuristic from dispatch.py without importing it (avoid cycle)."""
    cls_name = type(deps.pool).__name__.lower()
    if "lightpanda" in cls_name:
        return "lightpanda"
    return "cloak"


# ─── /profiles ──────────────────────────────────────────────────────
async def profiles_list(request: web.Request) -> web.Response:
    """``GET /profiles`` — list browser profiles, newest-touched first.

    Query: ?limit=N (default 100, clamped to [1, 500]).
    """
    store = request.app.get("profiles")
    if store is None or not store.enabled:
        return _json({"sessions": [], "error": "profiles disabled (no postgres)"}, status=503)
    limit = int(request.query.get("limit", "100") or "100")
    infos = await store.list(limit=limit)
    return _json({"profiles": [i.model_dump() for i in infos]})


async def profiles_get(request: web.Request) -> web.Response:
    """``GET /profiles/{name}`` — single profile metadata (NOT the zip)."""
    store = request.app.get("profiles")
    if store is None or not store.enabled:
        return _json({"error": "profiles disabled (no postgres)"}, status=503)
    name = request.match_info["name"]
    info = await store.get(name)
    if info is None:
        return _json({"error": "profile not found", "name": name}, status=404)
    return _json(info.model_dump())


async def profiles_fetch_zip(request: web.Request) -> web.Response:
    """``GET /profiles/{name}/zip`` — fetch the user_data zip binary.

    Returns ``application/octet-stream`` with the zip body, or 404 if
    no profile / no zip. Content-Disposition gives a sensible filename.
    """
    store = request.app.get("profiles")
    if store is None or not store.enabled:
        return _json({"error": "profiles disabled (no postgres)"}, status=503)
    name = request.match_info["name"]
    blob = await store.fetch_zip(name)
    if blob is None:
        return _json(
            {"error": "profile or zip not found", "name": name},
            status=404,
        )
    return web.Response(
        body=blob,
        content_type=_OCTET_STREAM,
        headers={
            "Content-Disposition": (f'attachment; filename="{name}.zip"'),
        },
    )


async def profiles_add(request: web.Request) -> web.Response:
    """``POST /profiles`` — create or replace a profile.

    Two body modes:

    Mode 1 — JSON body (no zip)::
        {
          "name": "alice",
          "backend": "cloak",
          "storage_state": {...},     // optional Playwright storage_state.json
          "metadata": {...}
        }

    Mode 2 — multipart upload of a zip::
        Content-Type: multipart/form-data
        Fields:
          name           form field
          backend        form field (optional, default "cloak")
          metadata       form field (optional, JSON string)
          profile        file part (the zip body)

    Returns the resulting profile info on 200; 400 on validation
    failures; 503 if postgres is unavailable.
    """
    store = request.app.get("profiles")
    if store is None or not store.enabled:
        return _json({"error": "profiles disabled (no postgres)"}, status=503)

    ct = request.headers.get("Content-Type", "")
    name: str | None = None
    backend = "cloak"
    storage_state: dict[str, Any] | None = None
    user_data_zip: bytes | None = None
    metadata: dict[str, Any] = {}

    if ct.startswith("multipart/"):
        # Mode 2: multipart with file upload.
        reader = await request.multipart()
        async for part in reader:
            if not isinstance(part, BodyPartReader):
                continue
            if part.name == "name":
                name = (await part.text()).strip()
            elif part.name == "backend":
                backend = (await part.text()).strip() or "cloak"
            elif part.name == "metadata":
                try:
                    metadata = orjson.loads(await part.read())
                except (orjson.JSONDecodeError, ValueError):
                    return _json({"error": "metadata: invalid json"}, status=400)
            elif part.name == "profile":
                user_data_zip = await part.read(decode=False)
            else:
                # Discard unknown parts.
                await part.read(decode=False)
    else:
        # Mode 1: JSON body.
        payload, err = await _read_json(request)
        if err:
            return _json({"error": err}, status=400)
        body = payload or {}
        name = (body.get("name") or "").strip() or None
        backend = (body.get("backend") or "cloak").strip()
        ss = body.get("storage_state")
        if ss is not None:
            if not isinstance(ss, dict):
                return _json(
                    {"error": "storage_state must be an object"},
                    status=400,
                )
            storage_state = ss
        md = body.get("metadata") or {}
        if not isinstance(md, dict):
            return _json({"error": "metadata must be an object"}, status=400)
        metadata = md

    if not name:
        return _json({"error": "name required"}, status=400)
    if backend not in ("cloak", "lightpanda", "browser-use"):
        backend = "cloak"

    try:
        info = await store.upsert(
            name=name,
            backend=backend,
            storage_state=storage_state,
            user_data_zip=user_data_zip,
            metadata=metadata,
        )
    except ValueError as e:
        return _json({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        return _json(
            {"error": f"{type(e).__name__}: {e}"},
            status=502,
        )
    return _json(info.model_dump(), status=200)


async def profiles_delete(request: web.Request) -> web.Response:
    """``DELETE /profiles/{name}`` — drop a profile + its blob."""
    store = request.app.get("profiles")
    if store is None or not store.enabled:
        return _json({"error": "profiles disabled (no postgres)"}, status=503)
    name = request.match_info["name"]
    deleted = await store.delete(name)
    if not deleted:
        return _json({"error": "profile not found"}, status=404)
    return _json({"ok": True, "name": name})


# ─── /chrome-conn + /vnc-conn (browser-use compat) ──────────────────
async def chrome_conn(request: web.Request) -> web.Response:
    """``GET /chrome-conn`` — return the CDP endpoint clients can drive.

    Backed by browser-use's CDP sidecar by default
    (``BROWSER_USE_CDP_URL``). Probes the URL for liveness; reports
    ``status="up"`` or ``status="down"`` so callers don't waste a CDP
    handshake against a dead endpoint.
    """
    from .browsers import browser_use_engine

    url = browser_use_engine.get_chrome_cdp_url()
    alive, detail = await browser_use_engine.probe_chrome_alive()
    return _json(
        {
            "status": "up" if alive else "down",
            "cdp_url": url,
            "detail": detail,
        }
    )


async def vnc_conn(request: web.Request) -> web.Response:
    """``GET /vnc-conn`` — return the VNC URL for visual debugging of
    the browser-use sidecar (when configured with VNC).

    No probe — VNC doesn't have a fast HTTP introspection endpoint.
    Operator confirms by opening the URL in a VNC viewer.
    """
    from .browsers import browser_use_engine

    return _json(
        {
            "vnc_url": browser_use_engine.get_vnc_url(),
            # Sidecar's web UI (web-ui project ships one on :8080).
            "web_ui_url": browser_use_engine.BROWSER_USE_HTTP_URL,
            "notes": (
                "Open this URL in a VNC viewer. The browser-use sidecar's "
                "web UI surfaces the live browser at "
                f"{browser_use_engine.BROWSER_USE_HTTP_URL}."
            ),
        }
    )


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
    # Profiles
    app.router.add_get("/profiles", profiles_list)
    app.router.add_post("/profiles", profiles_add)
    app.router.add_get("/profiles/{name}", profiles_get)
    app.router.add_get("/profiles/{name}/zip", profiles_fetch_zip)
    app.router.add_delete("/profiles/{name}", profiles_delete)
    # Browser-use sidecar conn URLs
    app.router.add_get("/chrome-conn", chrome_conn)
    app.router.add_get("/vnc-conn", vnc_conn)
    # Sessions
    app.router.add_post("/sessions", sessions_create)
    app.router.add_get("/sessions", sessions_list)
    app.router.add_get("/sessions/{id}", sessions_get)
    app.router.add_delete("/sessions/{id}", sessions_delete)


__all__ = [
    "crawl",
    "crawl_results",
    "crawl_status_route",
    "extract",
    "feeds_add",
    "feeds_del",
    "feeds_get",
    "feeds_list",
    "feeds_poll",
    "fetch_async",
    "fetch_sync",
    "health",
    "jobs",
    "register_routes",
    "screenshot",
    "sitemap_handler",
]
