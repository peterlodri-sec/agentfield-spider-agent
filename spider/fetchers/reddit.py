"""Reddit thread fetcher.

The shortlink form ``https://www.reddit.com/r/SUBREDDIT/s/SHORT`` is a
client-side redirect to the canonical
``/r/SUBREDDIT/comments/{id36}/.../?utm_*=...`` URL. We follow the
redirect, strip the tracking query params (those break the `.json`
endpoint with a 200 + non-JSON body), and request `.json` for the
structured payload.

Reddit's `.json` endpoint silently 429s / returns HTML for non-browser
User-Agents. We send a real Chrome UA — this is the same fix
``scripts/gather_projects.py`` had to apply when its initial run failed.
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..proxy import httpx_client_kwargs
from .base import USER_AGENT, error_result, ok_result


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def fetch_thread(url: str, *, proxy: str | None = None) -> dict[str, Any]:
    """Resolve a reddit URL (shortlink or canonical) to a thread record.

    Returns the dispatcher-standard dict with extras:
      - ``kind`` = ``"reddit"``
      - ``name`` = post title
      - ``description`` = ``"r/SUB thread, N pts, M comments, by u/AUTHOR"``
      - ``text`` = selftext (or hint when it's a link post)
      - ``source_url`` = original input URL
      - ``metadata`` = ``{subreddit, author, score, num_comments, link_url}``
    """
    t0 = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        **httpx_client_kwargs(proxy),
    ) as client:
        # Resolve to canonical URL by following the redirect chain.
        try:
            head = await client.get(url, follow_redirects=True)
        except httpx.RequestError as e:
            return error_result(
                url,
                error=f"redirect: {type(e).__name__}: {e}",
                t0=t0,
                extras={"kind": "reddit", "source_url": url},
            )

        canonical = _strip_query(str(head.url))
        json_url = canonical.rstrip("/") + ".json"

        # Pull the structured payload from the canonical URL.
        try:
            resp = await client.get(json_url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as e:
            return error_result(
                canonical,
                error=f"reddit json: {type(e).__name__}: {e}",
                t0=t0,
                extras={
                    "kind": "reddit",
                    "source_url": url,
                    "fetch_url": canonical,
                },
            )

    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return error_result(
            canonical,
            error="reddit json: unexpected payload shape",
            t0=t0,
            extras={"kind": "reddit", "source_url": url},
        )

    title = (post.get("title") or "").strip() or canonical
    subreddit = post.get("subreddit") or "?"
    author = post.get("author") or "?"
    score = post.get("score") or 0
    n_comments = post.get("num_comments") or 0
    selftext = (post.get("selftext") or "").strip()
    link_url = post.get("url") or ""
    is_self = bool(post.get("is_self"))

    if selftext:
        body_text: str | None = selftext
    elif link_url and not is_self:
        body_text = f"link post -> {link_url}"
    else:
        body_text = None

    description = (
        f"r/{subreddit} thread, {score} pts, "
        f"{n_comments} comments, by u/{author}"
    )

    return ok_result(
        canonical,
        title=title,
        text=body_text,
        t0=t0,
        extras={
            "kind": "reddit",
            "name": title,
            "description": description,
            "source_url": url,
            "metadata": {
                "subreddit": subreddit,
                "author": author,
                "score": score,
                "num_comments": n_comments,
                "link_url": link_url or None,
                "is_self": is_self,
            },
        },
    )


__all__ = ["fetch_thread"]
