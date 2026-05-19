"""Reasoner-layer wrappers for the spider agent's skills.

The ``@app.skill()`` functions in ``main.py`` are thin shims over these
helpers. The actual work happens in the HTTP surface
(``spider_server.py``), running in a sibling process / container.
``_fetch`` / ``_feed_add`` POST to ``$SPIDER_HTTP_URL`` (defaults to
``http://localhost:11700``, which is correct for the k3s sidecar layout;
compose overrides via ``.env``).

Why split the agent and HTTP surfaces into two processes?

  - The HTTP surface owns the heavy state (CloakBrowser pool + Redis
    connection + dispatcher / poller tasks). Running it in its own
    process keeps the AgentField agent restart-cheap.
  - Both surfaces use the exact same `spider/` package — feature
    parity is enforced by re-using `spider/routes.py` from the HTTP
    side, and just proxying from the AgentField side.

If your deployment puts the two processes in the same pod (the k3s
manifest does), `SPIDER_HTTP_URL=http://localhost:11700` is the right
default. In docker compose, the agent service can reach the HTTP
service via the bridge network (`http://spider-http:11700`).
"""

from __future__ import annotations

import os
from typing import Any, cast

import httpx

SPIDER_HTTP_URL = os.environ.get("SPIDER_HTTP_URL", "http://localhost:11700").rstrip("/")
TIMEOUT_S = float(os.environ.get("SPIDER_TIMEOUT_S", "60"))


def _client_factory() -> httpx.AsyncClient:
    """Return a fresh ``httpx.AsyncClient`` per call.

    Module-level test seams replace this attribute, so monkeypatching
    works against a stable name regardless of how httpx itself is
    imported elsewhere.
    """
    return httpx.AsyncClient(timeout=TIMEOUT_S)


def _response_json(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body from the HTTP surface."""
    data = response.json()
    if isinstance(data, dict):
        return cast(dict[str, Any], data)
    return {"status": "error", "error": "non-object json response"}


async def _fetch(
    url: str,
    sync: bool = True,
    extract: bool = True,  # noqa: ARG001 — accepted for API symmetry; v1 always extracts
) -> dict[str, Any]:
    """Proxy a fetch through the local HTTP surface.

    ``sync=True`` -> ``POST /fetch/sync`` (inline fetch, returns text).
    ``sync=False`` -> ``POST /fetch`` (enqueue, returns ``{queued, url}``).

    The return value is whatever the HTTP server returned, decoded as
    JSON. On HTTP transport errors we return a flat error dict so
    callers never see an exception from ``app.call`` — that matches the
    fleet convention (web-search-agent's `_web_search` does the same).
    """
    path = "/fetch/sync" if sync else "/fetch"
    try:
        async with _client_factory() as client:
            r = await client.post(
                f"{SPIDER_HTTP_URL}{path}",
                json={"url": url},
            )
            if r.status_code == 204:
                return {"status": "empty", "url": url}
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                    "url": url,
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "url": url,
        }


async def _feed_add(
    url: str,
    interval_s: int = 3600,
    name: str | None = None,
) -> dict[str, Any]:
    """Register an RSS/Atom feed via ``POST /feeds``.

    Returns the feed record on 200, an error dict on transport failure
    or non-2xx response.
    """
    body: dict[str, Any] = {"url": url}
    if interval_s:
        body["interval_s"] = int(interval_s)
    if name:
        body["name"] = str(name)
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/feeds", json=body)
            try:
                payload = _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                    "url": url,
                }
            if r.status_code >= 400:
                # Pass through server-side error structure (it carries
                # an `error` field) but ensure callers can branch on
                # `status_code`.
                payload.setdefault("status_code", r.status_code)
                return payload
            return payload
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "url": url,
        }


async def _feed_list() -> dict[str, Any]:
    """List all registered feeds."""
    try:
        async with _client_factory() as client:
            r = await client.get(f"{SPIDER_HTTP_URL}/feeds")
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _feed_remove(feed_id: str) -> dict[str, Any]:
    """Remove a feed by ID."""
    try:
        async with _client_factory() as client:
            r = await client.delete(f"{SPIDER_HTTP_URL}/feeds/{feed_id}")
            try:
                payload = _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
            if r.status_code >= 400:
                payload.setdefault("status_code", r.status_code)
            return payload
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _feed_poll(feed_id: str) -> dict[str, Any]:
    """Force-poll a feed."""
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/feeds/{feed_id}/poll")
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _jobs_recent(limit: int = 50) -> dict[str, Any]:
    """List recent fetch jobs."""
    try:
        async with _client_factory() as client:
            r = await client.get(f"{SPIDER_HTTP_URL}/jobs?limit={limit}")
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _crawl(
    url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    same_domain_only: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Start a recursive crawl."""
    body = {
        "url": url,
        "max_depth": max_depth,
        "max_pages": max_pages,
        "same_domain_only": same_domain_only,
        "timeout_seconds": timeout_seconds,
    }
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/crawl", json=body)
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _crawl_status(job_id: str) -> dict[str, Any]:
    """Get crawl job status."""
    try:
        async with _client_factory() as client:
            r = await client.get(f"{SPIDER_HTTP_URL}/crawl/{job_id}")
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _sitemap_parse(
    url: str,
    max_urls: int = 1000,
    queue: bool = False,
) -> dict[str, Any]:
    """Parse a sitemap."""
    body = {"url": url, "max_urls": max_urls, "queue": queue}
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/sitemap/parse", json=body)
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _screenshot(
    url: str,
    full_page: bool = True,
) -> dict[str, Any]:
    """Take a screenshot."""
    body = {"url": url, "full_page": full_page}
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/screenshot", json=body)
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _extract(
    url: str,
    selector: str,
    attr: str | None = None,
) -> dict[str, Any]:
    """Extract content via CSS selector."""
    body = {"url": url, "selector": selector}
    if attr:
        body["attr"] = attr
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/extract", json=body)
            try:
                return _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                }
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


async def _act(
    task: str,
    session_id: str,
    *,
    model: str | None = None,
    max_steps: int | None = None,
    allowed_hosts: list[str] | None = None,
) -> dict[str, Any]:
    """Run a browser-use Agent on ``task`` inside ``session_id``'s context.

    Proxies ``POST /act`` on the HTTP surface. Returns the same flat
    dict shape the server returns: ``status``, ``task``, ``session_id``,
    ``model``, ``ms``, optionally ``result`` / ``urls_visited`` /
    ``steps_taken`` / ``is_done`` on success, or ``error`` on failure.
    """
    body: dict[str, Any] = {"task": task, "session_id": session_id}
    if model:
        body["model"] = model
    if max_steps is not None:
        body["max_steps"] = int(max_steps)
    if allowed_hosts:
        body["allowed_hosts"] = list(allowed_hosts)
    try:
        async with _client_factory() as client:
            r = await client.post(f"{SPIDER_HTTP_URL}/act", json=body)
            try:
                payload = _response_json(r)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"non-json response: HTTP {r.status_code}",
                    "task": task,
                    "session_id": session_id,
                }
            if r.status_code >= 400:
                payload.setdefault("status_code", r.status_code)
            return payload
    except httpx.RequestError as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "task": task,
            "session_id": session_id,
        }


__all__ = [
    "_act",
    "_crawl",
    "_crawl_status",
    "_extract",
    "_feed_add",
    "_feed_list",
    "_feed_poll",
    "_feed_remove",
    "_fetch",
    "_jobs_recent",
    "_screenshot",
    "_sitemap_parse",
    "SPIDER_HTTP_URL",
    "TIMEOUT_S",
]
