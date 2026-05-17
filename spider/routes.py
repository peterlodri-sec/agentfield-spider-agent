"""aiohttp route handlers.

Mounted by ``spider_server.build_app``. Only the synchronous fetch
surface and a health probe in PR 1; the async queue + feeds + jobs
routes land in PR 2.

Each handler is a thin shim: validate input, look up shared deps via
``request.app[...]``, delegate to ``spider.fetch.fetch_one``, shape
the return into the expected HTTP body + status.
"""
from __future__ import annotations

import os
from typing import Any

import orjson
from aiohttp import web

from .fetch import FetchDeps, fetch_one
from .log import log
from .security import is_safe_external_url


_CONTENT_JSON = "application/json"


def _json(obj: Any, status: int = 200) -> web.Response:
    """Stable JSON body via orjson; avoids the per-call dict shape
    differences that aiohttp's ``web.json_response`` introduces.
    """
    return web.Response(
        body=orjson.dumps(obj),
        status=status,
        content_type=_CONTENT_JSON,
    )


async def health(request: web.Request) -> web.Response:
    """Liveness + readiness probe.

    Reports the open/closed state of the browser pool and a redacted
    summary of the env knobs that downstream callers might want to
    sanity-check. PR 2 expands this with Redis queue / feeds / jobs
    counters.
    """
    pool = request.app.get("browser")
    browser_state = "absent"
    if pool is not None:
        browser_state = "open" if pool._browser is not None else "closed"  # noqa: SLF001
    return _json({
        "ok": True,
        "service": "spider-http",
        "browser": browser_state,
        "fetch_timeout_ms": int(os.environ.get("FETCH_TIMEOUT_MS", "30000")),
    })


async def fetch_sync(request: web.Request) -> web.Response:
    """``POST /fetch/sync`` — fetch + extract inline; return text.

    Request body: ``{"url": "https://..."}``.

    Response (200) on success::

        {
            "id": <job id>, "url": <input url>,
            "fetch_url": <resolved url>,
            "title": <str | null>, "text": <str>,
            "status": "ok", "ms": <int>, "ts": <int>
        }

    Non-200 outcomes carry ``status`` and ``error`` fields:
      400 — URL parse / SSRF / redirect-SSRF
      403 — blocked-by-robots
      502 — fetch/extract error
      204 — empty content (no body)
    """
    try:
        body = await request.json(loads=orjson.loads)
    except Exception:
        return _json({"error": "invalid json body"}, status=400)
    url = (body or {}).get("url", "") if isinstance(body, dict) else ""
    if not isinstance(url, str) or not url:
        return _json({"error": "url required"}, status=400)

    # Layer-1 SSRF gate (cheap; rejects literal-host loopback / private
    # IPs without DNS). Layer 2 runs inside fetch_one.
    if not is_safe_external_url(url):
        log("ssrf", url=url, gate="layer1")
        return _json(
            {"error": "url failed SSRF gate", "url": url},
            status=400,
        )

    deps: FetchDeps = request.app["deps"]
    result = await fetch_one(deps, url)
    status = result.get("status")
    if status == "ok":
        return _json(result, status=200)
    if status == "empty":
        return _json(result, status=204)
    if status == "blocked-by-robots":
        return _json(result, status=403)
    if status == "blocked-by-ssrf-gate":
        return _json(result, status=400)
    # "error" or unknown.
    return _json(result, status=502)


def register_routes(app: web.Application) -> None:
    """Wire the v1 routes into ``app``.

    Called from ``spider_server.build_app``. Idempotent — registering a
    duplicate route raises, so this is the only place we set them.
    """
    app.router.add_get("/health", health)
    app.router.add_post("/fetch/sync", fetch_sync)


__all__ = [
    "fetch_sync",
    "health",
    "register_routes",
]
