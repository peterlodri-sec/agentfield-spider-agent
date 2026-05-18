"""URL-kind dispatcher.

Routes incoming fetch requests to the right specialized fetcher based
on URL shape. Generic URLs fall through to the existing browser-rendered
``fetch_one()`` path in ``spider/fetch.py``.

The point isn't speed (most pages would render fine via CloakBrowser);
it's signal-quality. GitHub's REST API gives us the canonical repo
description + README in two HTTP calls, more reliably than scraping the
HTML. HN's Firebase API gives the score + comments + linked URL.
Reddit's `.json` shape gives the title + selftext without rendering an
SPA. For document URLs (PDF, Word, PowerPoint, etc.), Docling extracts
structured Markdown + tables instead of asking Chromium to render a
single canvas with no extractable text.

Each specialized fetcher returns the same flat-dict shape as
``fetch_one``: ``{id, url, fetch_url, status, title, text, ms, ts}``
plus optional richer fields (``kind``, ``description``, ``name``,
``metadata``). That keeps the routes layer + jobs ZSET + AgentField
skill response stable — callers expecting ``text`` keep working.

Browser-backend choice
----------------------

For the generic (browser-rendered) path, callers can pick which
backend renders: ``browser="cloak"`` (default, stealth Chromium) or
``browser="lightpanda"`` (beta, Zig CDP browser). The choice flows
through ``spider/browsers/get_or_create_pool`` which lazy-instantiates
the alternate pool on first use, so the unused backend has zero cost.
"""
from __future__ import annotations

import re
from typing import Any

from . import fetchers as fetchers_pkg
from .browsers import Backend, get_or_create_pool, normalize_backend
from .docling_parser import fetch_via_docling, is_docling_url
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
    | ``docling`` | ``generic``.

    Pure function — no I/O, no expensive imports at call time (the
    docling check is a regex extension test, not the docling import).
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
    if is_docling_url(url):
        return "docling"
    return "generic"


# ─── dispatch ────────────────────────────────────────────────────────
async def dispatch_fetch(
    deps: FetchDeps,
    url: str,
    *,
    use_browser: bool = False,
    proxy: str | None = None,
    browser: str | None = None,
) -> dict[str, Any]:
    """Route ``url`` to the right fetcher.

    Args
    ----
    use_browser  If True, skip the per-kind specialized fetcher and
                 use the browser-rendered ``fetch_one()`` path. Use
                 when the caller knows the page is JS-heavy or wants
                 a real DOM render of e.g. a GitHub repo page.
    proxy        Overrides ``SPIDER_PROXY_URL`` for this call. Only
                 honored by the specialized HTTP fetchers; the
                 browser path's proxy is fixed at pool launch.
    browser      Browser backend for the browser-rendered path:
                 ``"cloak"`` (default) or ``"lightpanda"`` (beta).
                 Unknown values fall back to ``"cloak"``. Lazy: the
                 alternate pool only gets constructed on first use.

    Returns the same flat-dict shape as ``fetch_one``. Errors are
    captured into ``status="error"`` rather than raised.
    """
    chosen_backend: Backend = normalize_backend(browser)

    if use_browser:
        return await _run_browser_path(deps, url, backend=chosen_backend)

    kind = classify(url)
    if kind == "github":
        return await fetchers_pkg.github.fetch_repo(url, proxy=proxy)
    if kind == "github-issue":
        return await fetchers_pkg.github.fetch_issue(url, proxy=proxy)
    if kind == "hn":
        return await fetchers_pkg.hn.fetch_item(url, proxy=proxy)
    if kind == "reddit":
        return await fetchers_pkg.reddit.fetch_thread(url, proxy=proxy)
    if kind == "docling":
        return await fetch_via_docling(url, proxy=proxy)
    # ``generic`` -> browser-rendered path.
    return await _run_browser_path(deps, url, backend=chosen_backend)


async def _run_browser_path(
    deps: FetchDeps,
    url: str,
    *,
    backend: Backend,
) -> dict[str, Any]:
    """Run ``fetch_one`` with the requested browser backend.

    The default (env-configured) pool lives on ``deps.pool``. If the
    caller requested a different backend, lazy-construct it via the
    registry and pass it as a per-call override.
    """
    # If the requested backend matches the primary pool, just use it.
    if backend == _primary_backend(deps):
        return await fetch_one(deps, url)
    alt_pool = await get_or_create_pool(backend)
    return await fetch_one(deps, url, pool=alt_pool)


def _primary_backend(deps: FetchDeps) -> Backend:
    """Best-effort detect of which backend the primary pool belongs to.

    Looks at the pool class name. We avoid importing the pool classes
    here to keep dispatch.py light; the comparison is by class name.
    """
    cls_name = type(deps.pool).__name__.lower()
    if "lightpanda" in cls_name:
        return "lightpanda"
    return "cloak"


__all__ = ["classify", "dispatch_fetch"]
