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
from typing import Any

import httpx

SPIDER_HTTP_URL = os.environ.get("SPIDER_HTTP_URL", "http://localhost:11700").rstrip("/")
TIMEOUT_S = float(os.environ.get("SPIDER_TIMEOUT_S", "60"))


def _client_factory():
    """Return a fresh ``httpx.AsyncClient`` per call.

    Module-level test seams replace this attribute, so monkeypatching
    works against a stable name regardless of how httpx itself is
    imported elsewhere.
    """
    return httpx.AsyncClient(timeout=TIMEOUT_S)


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
                return r.json()
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
                payload = r.json()
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


__all__ = ["_fetch", "_feed_add", "SPIDER_HTTP_URL", "TIMEOUT_S"]
