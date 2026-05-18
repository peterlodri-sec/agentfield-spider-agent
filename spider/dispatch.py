"""URL-kind dispatcher.

Routes incoming fetch requests to the right specialized fetcher based
on URL shape. Generic URLs fall through to the existing browser-rendered
``fetch_one()`` path in ``spider/fetch.py``.

The point isn't speed (most pages would render fine via CloakBrowser);
it's signal-quality. GitHub's REST API gives us the canonical repo
description + README in two HTTP calls, more reliably than scraping the
HTML. HN's Firebase API gives the score + comments + linked URL.
Reddit's `.json` shape gives the title + selftext without rendering an
SPA. For each, the structured response is a strict superset of what a
browser fetch would extract.

Each specialized fetcher returns the same flat-dict shape as
``fetch_one``: ``{id, url, fetch_url, status, title, text, ms, ts}``
plus optional richer fields (``kind``, ``description``, ``name``,
``metadata``). That keeps the routes layer + jobs ZSET + AgentField
skill response stable — callers expecting ``text`` keep working.
"""
from __future__ import annotations

import re
from typing import Any

from . import fetchers as fetchers_pkg
from .fetch import FetchDeps, fetch_one


# ─── classification ──────────────────────────────────────────────────
_GH_ISSUE_RE = re.compile(
    r"^https?://github\.com/[^/]+/[^/]+/issues/\d+", re.I,
)
_GH_REPO_RE = re.compile(
    r"^https?://github\.com/[^/]+/[^/]+/?(?:[?#].*)?$", re.I,
)
_HN_RE = re.compile(r"^https?://news\.ycombinator\.com/", re.I)
_REDDIT_RE = re.compile(r"^https?://(?:www\.|old\.)?reddit\.com/", re.I)


def classify(url: str) -> str:
    """Return one of: ``github`` | ``github-issue`` | ``hn`` | ``reddit``
    | ``generic``.

    Pure function — no I/O, no allocations beyond regex match. Safe to
    call in hot paths.
    """
    if not isinstance(url, str) or not url:
        return "generic"
    if _GH_ISSUE_RE.match(url):
        return "github-issue"
    if _GH_REPO_RE.match(url):
        return "github"
    if _HN_RE.search(url):
        return "hn"
    if _REDDIT_RE.search(url):
        return "reddit"
    return "generic"


# ─── dispatch ────────────────────────────────────────────────────────
async def dispatch_fetch(
    deps: FetchDeps,
    url: str,
    *,
    use_browser: bool = False,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Route ``url`` to the right fetcher.

    ``use_browser=True`` forces the generic browser-rendered path
    regardless of URL kind. Use this when the caller knows the page
    is JS-heavy or expects a real DOM render.

    ``proxy`` overrides ``SPIDER_PROXY_URL`` for this call. Only honored
    by the specialized HTTP fetchers; the browser path uses the env
    proxy (pool is shared, can't swap per call).

    Returns the same flat-dict shape as ``fetch_one``. Errors are
    captured into ``status="error"`` rather than raised.
    """
    if use_browser:
        # Skip the dispatcher; go straight to the browser pipeline.
        # NOTE: fetch_one doesn't accept per-call proxy yet (browser
        # pool is shared); the env proxy applies if set.
        return await fetch_one(deps, url)

    kind = classify(url)
    if kind == "github":
        return await fetchers_pkg.github.fetch_repo(url, proxy=proxy)
    if kind == "github-issue":
        return await fetchers_pkg.github.fetch_issue(url, proxy=proxy)
    if kind == "hn":
        return await fetchers_pkg.hn.fetch_item(url, proxy=proxy)
    if kind == "reddit":
        return await fetchers_pkg.reddit.fetch_thread(url, proxy=proxy)
    # ``generic`` (or anything we didn't recognize) -> browser pipeline.
    return await fetch_one(deps, url)


__all__ = ["classify", "dispatch_fetch"]
