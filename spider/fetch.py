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

from .browser import DISABLE_RESOURCES, FETCH_TIMEOUT_MS, NETWORK_IDLE
from .browsers.base import BrowserPool
from .errors import (
    STATUS_OK,
    RobotsBlockedError,
    SpiderBlockedError,
    SpiderEmptyContentError,
    SpiderError,
    SpiderFetchError,
    SSRFBlockedError,
    outcome_from_exception,
)
from .extract import dismiss_banners, extract_title_text
from .log import log, log_exception
from .politeness import HostBucket, RobotsCache, host_of
from .security import follow_safe_redirects, is_safe_external_url_with_dns


@dataclass(slots=True, frozen=True)
class FetchDeps:
    """Bag of long-lived dependencies the fetch pipeline needs.

    Constructed once in ``spider_server.build_app`` (and the future
    ``main.py`` agent surface) and passed to ``fetch_one`` per call.

    ``sessions`` is the optional SessionManager for per-context
    persistence — see ``spider/sessions.py``. When None (default),
    callers can still pass session_id but it's a no-op (anonymous
    lease).
    """

    http: aiohttp.ClientSession
    pool: BrowserPool
    robots: RobotsCache
    bucket: HostBucket
    sessions: Any = None  # Optional[SessionManager]


# ─── URL rewrites ────────────────────────────────────────────────────
_GH_REPO_RE = re.compile(
    r"^(https?://github\.com)/([^/?#]+)/([^/?#]+?)(?:\.git)?/?(?:[?#].*)?$",
    re.I,
)
_GH_NON_REPO_OWNERS = frozenset(
    {
        "trending",
        "search",
        "marketplace",
        "settings",
        "about",
        "pricing",
        "topics",
        "explore",
        "features",
        "team",
        "enterprise",
        "events",
        "customer-stories",
        "readme",
        "sponsors",
        "collections",
        "site",
        "orgs",
        "organizations",
        "login",
        "join",
        "new",
        "issues",
        "pulls",
        "notifications",
        "watching",
        "stars",
    }
)


@lru_cache(maxsize=2048)
def rewrite_url(url: str) -> tuple[str, str | None]:
    """Return ``(effective_url, rewrite_kind | None)``."""
    m = _GH_REPO_RE.match(url)
    if m and m.group(2).lower() not in _GH_NON_REPO_OWNERS:
        host, owner, repo = m.group(1), m.group(2), m.group(3)
        return (
            f"{host}/{owner}/{repo}/raw/HEAD/README.md",
            "github-readme",
        )
    return url, None


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def _fetch_one_core(
    deps: FetchDeps,
    url: str,
    base: dict[str, Any],
    t0: float,
    *,
    pool: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run the fetch pipeline; raises ``SpiderError`` subclasses on policy failures."""
    fetch_url, rewrite_kind = rewrite_url(url)
    base["fetch_url"] = fetch_url
    if rewrite_kind:
        log("rewrite", from_=url, to=fetch_url, kind=rewrite_kind)

    if not await is_safe_external_url_with_dns(fetch_url):
        log("ssrf", url=fetch_url, ms=_elapsed_ms(t0))
        raise SSRFBlockedError()

    robots_blocked_url = ""

    async def _hop_policy(hop_url: str) -> bool:
        nonlocal robots_blocked_url
        if not await deps.robots.allowed(hop_url):
            robots_blocked_url = hop_url
            return False
        await deps.bucket.acquire(host_of(hop_url))
        return True

    final_url = await follow_safe_redirects(
        deps.http,
        fetch_url,
        hop_policy=_hop_policy,
    )
    if final_url is None:
        ms = _elapsed_ms(t0)
        if robots_blocked_url:
            log("robots", url=robots_blocked_url, blocked=True, ms=ms)
            raise RobotsBlockedError()
        log("ssrf", url=fetch_url, reason="redirect", ms=ms)
        raise SSRFBlockedError("url failed SSRF gate (redirect)")

    if final_url != fetch_url:
        log("redirect-resolved", from_=fetch_url, to=final_url)
        fetch_url = final_url
        base["fetch_url"] = fetch_url

    await deps.bucket.acquire(host_of(fetch_url))

    active_pool = pool if pool is not None else deps.pool

    # Optional per-session evasion layer. Cheap lookup; returns None
    # when no session_id was passed, the session has no evasion, or
    # the SessionManager itself is unavailable.
    evasion = None
    if session_id and deps.sessions is not None:
        evasion = await deps.sessions.get_evasion(session_id)

    try:
        if session_id and deps.sessions is not None:
            lease_cm = deps.sessions.lease_page(session_id, pool=active_pool)
        else:
            lease_cm = active_pool.lease()
        async with lease_cm as page:
            # Per-session rate gate (no-op when evasion is None or
            # rate_per_min <= 0). Runs BEFORE goto so the bucket
            # protects the goto itself.
            if evasion is not None:
                await evasion.check_rate()
            await page.goto(
                fetch_url,
                wait_until="networkidle" if NETWORK_IDLE else "load",
                timeout=FETCH_TIMEOUT_MS,
            )
            # Light behavioral simulation after goto succeeds.
            # No-op when evasion is None or behavioral_dwell is False.
            if evasion is not None:
                await evasion.post_goto(page)
            await dismiss_banners(page)
            title, text = await extract_title_text(page)
    except SpiderError:
        raise
    except Exception as e:
        raise SpiderFetchError(f"{type(e).__name__}: {e}") from e

    ms = _elapsed_ms(t0)
    log(
        "fetch",
        url=fetch_url,
        chars=len(text),
        title=title or "(no title)",
        ms=ms,
    )

    if not text or len(text) < 100:
        raise SpiderEmptyContentError()

    return {
        **base,
        "status": STATUS_OK,
        "title": title or None,
        "text": text,
        "ms": ms,
    }


async def fetch_one(
    deps: FetchDeps,
    url: str,
    *,
    pool: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Fetch a URL through the browser pool and return a job-record dict.

    Always returns; never raises.
    """
    job_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    base: dict[str, Any] = {
        "id": job_id,
        "url": url,
        "ts": int(time.time() * 1000),
    }

    try:
        return await _fetch_one_core(
            deps,
            url,
            base,
            t0,
            pool=pool,
            session_id=session_id,
        )
    except SpiderBlockedError as e:
        ms = _elapsed_ms(t0)
        log_exception("blocked", e, url=base.get("fetch_url", url), ms=ms)
        return outcome_from_exception(base, e, ms=ms)
    except SpiderError as e:
        ms = _elapsed_ms(t0)
        log_exception("fetch-outcome", e, url=base.get("fetch_url", url), ms=ms)
        return outcome_from_exception(base, e, ms=ms)
    except Exception as e:
        ms = _elapsed_ms(t0)
        log_exception("error", e, url=base.get("fetch_url", url), where="fetch", ms=ms)
        return outcome_from_exception(base, e, ms=ms)


_ = DISABLE_RESOURCES


__all__ = [
    "FetchDeps",
    "fetch_one",
    "rewrite_url",
]
