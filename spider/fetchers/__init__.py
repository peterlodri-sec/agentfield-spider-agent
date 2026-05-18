"""Specialized fetchers, one per recognized URL kind.

Each module exposes ``async def fetch_*(url, *, proxy=None)`` returning
the same flat-dict shape as ``spider.fetch.fetch_one`` so the
dispatcher in ``spider/dispatch.py`` can hand back whichever fetcher
result the caller's URL routed to.

Modules:

  github   GitHub repo + issue via REST API (httpx + optional
           GITHUB_TOKEN for higher rate limits).
  hn       Hacker News item via the public Firebase API; recurses into
           the github fetcher when the linked URL is a github repo so
           HN posts that link to repos surface the repo metadata.
  reddit   Reddit thread via the `.json` endpoint, after following the
           shortlink redirect and stripping tracking query params.
           Browser-like User-Agent (reddit blocks the urllib default).
"""
from __future__ import annotations

from . import github, hn, reddit

__all__ = ["github", "hn", "reddit"]
