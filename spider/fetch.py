"""Core fetch pipeline.

Single entry point: ``fetch_one(deps, url)``. Ports rag-stack's
``_fetch_one`` minus the topic quality gate, the daily-pages cap, and
the rag-pocket save. Behaviour preserved:

  - URL rewrite (e.g. github.com/foo/bar -> raw README).
  - Two-layer SSRF gate (literal-host + DNS).
  - Pre-walk redirect chain with per-hop robots + bucket checks.
  - One token of per-host bucket for the actual browser fetch.
  - CloakBrowser page open, optional network-idle wait.
  - Cookie-banner dismissal, then extract (title + text).
  - Empty-content gate (< 100 chars => "empty" status).

Returns a flat dict the routes layer can shape into either an HTTP
response or a Redis job record. The dict's ``status`` field is the
canonical signal: ``"ok"``, ``"empty"``, ``"blocked-by-ssrf-gate"``,
``"blocked-by-robots"``, or ``"error"``.

Async-queue plumbing (Redis queue + dispatcher worker + jobs ZSET) is
out of scope for this module — that lands in PR 2 (``spider/queue.py``).
PR 1 keeps the same fetch_one shape so PR 2 wraps it without churn.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import aiohttp

from .browser import (
    CloakBrowserPool, DISABLE_RESOURCES, FETCH_TIMEOUT_MS, NETWORK_IDLE,
)
from .extract import dismiss_banners, extract_title_text
from .log import log
from .politeness import HostBucket, RobotsCache, host_of
from .security import follow_safe_redirects, is_safe_external_url_with_dns


@dataclass(slots=True, frozen=True)
class FetchDeps:
    """Bag of long-lived dependencies the fetch pipeline needs.

    Constructed once in ``spider_server.build_app`` (and the future
    ``main.py`` agent surface) and passed to ``fetch_one`` per call.
    """

    http: aiohttp.ClientSession
    pool: CloakBrowserPool
    robots: RobotsCache
    bucket: HostBucket


# ─── URL rewrites ────────────────────────────────────────────────────
# GitHub repo URLs are heavy (~3 MB of HTML for chrome we don't index).
# Redirect to the raw README first; the /raw/HEAD/README.md path
# 302-redirects to raw.githubusercontent.com and the browser follows it.
_GH_REPO_RE = re.compile(
    r"^(https?://github\.com)/([^/?#]+)/([^/?#]+?)(?:\.git)?/?(?:[?#].*)?$",
    re.I,
)
_GH_NON_REPO_OWNERS = frozenset({
    "trending", "search", "marketplace", "settings", "about", "pricing",
    "topics", "explore", "features", "team", "enterprise", "events",
    "customer-stories", "readme", "sponsors", "collections", "site",
    "orgs", "organizations", "login", "join", "new", "issues", "pulls",
    "notifications", "watching", "stars",
})


@lru_cache(maxsize=2048)
def rewrite_url(url: str) -> tuple[str, str | None]:
    """Return ``(effective_url, rewrite_kind | None)``.

    Idempotent and deterministic. Currently only rewrites
    ``github.com/<owner>/<repo>`` to its raw README.
    """
    m = _GH_REPO_RE.match(url)
    if m and m.group(2).lower() not in _GH_NON_REPO_OWNERS:
        host, owner, repo = m.group(1), m.group(2), m.group(3)
        return (
            f"{host}/{owner}/{repo}/raw/HEAD/README.md",
            "github-readme",
        )
    return url, None


# ─── core fetch ──────────────────────────────────────────────────────
async def fetch_one(deps: FetchDeps, url: str) -> dict[str, Any]:
    """Fetch a URL through CloakBrowser and return a job-record dict.

    Always returns; never raises. Callers decide whether to surface an
    error as a 4xx/5xx (sync surface) or just record it (async surface).

    Return shape::

        {
            "id": <12-char job id>,
            "url": <original url>,
            "fetch_url": <after rewrite + redirect resolution>,
            "status": "ok" | "empty" | "blocked-by-ssrf-gate"
                    | "blocked-by-robots" | "error",
            "title": <str | None>,    # only when status == "ok"
            "text": <str | None>,     # only when status == "ok"
            "ms": <int milliseconds>,
            "ts": <int ms-epoch>,
            "error": <str | None>,    # only when status == "error" or
                                       # blocked-*
        }
    """
    job_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    ts = int(time.time() * 1000)
    base: dict[str, Any] = {
        "id": job_id,
        "url": url,
        "ts": ts,
    }

    # URL rewrite (idempotent).
    fetch_url, rewrite_kind = rewrite_url(url)
    base["fetch_url"] = fetch_url
    if rewrite_kind:
        log("rewrite", from_=url, to=fetch_url, kind=rewrite_kind)

    # Layer-2 SSRF gate on the (possibly rewritten) URL.
    if not await is_safe_external_url_with_dns(fetch_url):
        ms = int((time.perf_counter() - t0) * 1000)
        log("ssrf", url=fetch_url, ms=ms)
        return {
            **base,
            "status": "blocked-by-ssrf-gate",
            "error": "url failed SSRF gate",
            "ms": ms,
        }

    # Pre-walk redirect chain. Every hop runs the SSRF gate; the
    # hop_policy callback adds robots + per-host bucket checks so the
    # walker observes the same politeness policy as the browser fetch.
    robots_blocked = {"url": ""}

    async def _hop_policy(hop_url: str) -> bool:
        if not await deps.robots.allowed(hop_url):
            robots_blocked["url"] = hop_url
            return False
        await deps.bucket.acquire(host_of(hop_url))
        return True

    final_url = await follow_safe_redirects(
        deps.http, fetch_url, hop_policy=_hop_policy,
    )
    if final_url is None:
        ms = int((time.perf_counter() - t0) * 1000)
        if robots_blocked["url"]:
            log("robots", url=robots_blocked["url"], blocked=True, ms=ms)
            return {
                **base,
                "status": "blocked-by-robots",
                "error": "blocked by robots.txt",
                "ms": ms,
            }
        log("ssrf", url=fetch_url, reason="redirect", ms=ms)
        return {
            **base,
            "status": "blocked-by-ssrf-gate",
            "error": "url failed SSRF gate (redirect)",
            "ms": ms,
        }
    if final_url != fetch_url:
        log("redirect-resolved", from_=fetch_url, to=final_url)
        fetch_url = final_url
        base["fetch_url"] = fetch_url

    # The hop_policy already acquired a bucket token for the final
    # URL's host (the walker's own GET consumed it). Acquire one more
    # token for the actual browser fetch.
    await deps.bucket.acquire(host_of(fetch_url))

    try:
        async with deps.pool.lease() as page:
            await page.goto(
                fetch_url,
                wait_until="networkidle" if NETWORK_IDLE else "load",
                timeout=FETCH_TIMEOUT_MS,
            )
            await dismiss_banners(page)
            title, text = await extract_title_text(page)
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        err = f"{type(e).__name__}: {e}"
        log("error", url=fetch_url, where="fetch", msg=err, ms=ms)
        return {
            **base,
            "status": "error",
            "error": err,
            "ms": ms,
        }

    ms = int((time.perf_counter() - t0) * 1000)
    log(
        "fetch", url=fetch_url, chars=len(text),
        title=title or "(no title)", ms=ms,
    )

    if not text or len(text) < 100:
        return {
            **base,
            "status": "empty",
            "ms": ms,
        }

    return {
        **base,
        "status": "ok",
        "title": title or None,
        "text": text,
        "ms": ms,
    }


# Keep DISABLE_RESOURCES on the public surface in case the CloakBrowser
# wrapper accepts it as a launch arg in a later version (used to thread
# through to Scrapling's resource-blocking). v1 doesn't actively pass
# it but documenting the env knob keeps deploy/k3s ConfigMap stable.
_ = DISABLE_RESOURCES


__all__ = [
    "FetchDeps",
    "fetch_one",
    "rewrite_url",
]
