"""Hacker News item fetcher.

Public Firebase API:
  https://hacker-news.firebaseio.com/v0/item/{id}.json

Returns the linked URL + title + score + comments + author. If the
linked URL is a GitHub repo, we recurse into the github fetcher so the
caller gets the same shape as fetching that repo directly (with HN
context preserved in the description).

For non-repo linked URLs we fetch the article HTML and extract a
title + 500-char text excerpt. Ask HN / Show HN posts with selftext
use the item's ``text`` field directly (no second fetch).
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..proxy import httpx_client_kwargs
from .base import (
    USER_AGENT,
    error_result,
    extract_html_title,
    ok_result,
    strip_html,
)

_ID_RE = re.compile(r"\bid=(\d+)")
_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/[^/]+/[^/?#]+/?(?:[?#].*)?$", re.I,
)


async def fetch_item(url: str, *, proxy: str | None = None) -> dict[str, Any]:
    """Fetch one HN item by its ?id=N query param.

    Recurses into ``github.fetch_repo`` when the linked URL is a repo;
    otherwise fetches the article and extracts an excerpt.
    """
    t0 = time.perf_counter()
    m = _ID_RE.search(url)
    if not m:
        return error_result(url, error="no id in hn url", t0=t0)
    item_id = m.group(1)

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": USER_AGENT},
        **httpx_client_kwargs(proxy),
    ) as client:
        try:
            resp = await client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json",
            )
            resp.raise_for_status()
            item = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as e:
            return error_result(
                url, error=f"hn api: {type(e).__name__}: {e}", t0=t0,
            )

        if not isinstance(item, dict):
            return error_result(
                url, error="hn api returned non-dict", t0=t0,
            )

        title = (item.get("title") or "").strip()
        linked = item.get("url") or ""
        author = item.get("by") or "?"
        score = item.get("score") or 0
        n_comments = item.get("descendants") or 0

        # Path 1: linked URL is a github repo -> recurse into github fetcher.
        if linked and _GITHUB_REPO_RE.match(linked):
            # Local import to avoid circulating the fetchers package on
            # cold start (fetchers/__init__ imports both modules; the
            # cross-module call here only happens during a fetch).
            from . import github as github_mod

            sub = await github_mod.fetch_repo(linked, proxy=proxy)
            sub["kind"] = "github-via-hn"
            sub["source_url"] = url
            existing_desc = (sub.get("description") or "")
            sub["description"] = (
                f"[HN: \"{title}\" — {score} pts, "
                f"{n_comments} comments, by {author}] "
                + (existing_desc or "")
            ).strip()
            sub["metadata"] = {
                **(sub.get("metadata") or {}),
                "hn_id": int(item_id),
                "hn_score": score,
                "hn_comments": n_comments,
                "hn_author": author,
            }
            # Preserve ms-from-now timing on the outer call.
            sub["ms"] = int((time.perf_counter() - t0) * 1000)
            return sub

        # Path 2: no linked URL -> Ask/Show HN with selftext.
        if not linked:
            selftext = strip_html(item.get("text") or "")
            return ok_result(
                url,
                title=title or f"HN item #{item_id}",
                text=selftext or None,
                t0=t0,
                extras={
                    "kind": "hn",
                    "name": title or f"HN#{item_id}",
                    "description": (
                        f"HN: {score} pts, {n_comments} comments, by {author}"
                    ),
                    "source_url": url,
                    "metadata": {
                        "hn_id": int(item_id),
                        "hn_score": score,
                        "hn_comments": n_comments,
                        "hn_author": author,
                    },
                },
            )

        # Path 3: linked to an article -> fetch + extract title + excerpt.
        excerpt = ""
        article_title = ""
        try:
            ar = await client.get(linked, timeout=15.0, follow_redirects=True)
            if ar.status_code < 400 and ar.headers.get(
                "Content-Type", "",
            ).startswith(("text/", "application/xhtml")):
                html = ar.text[:200_000]  # cap big pages
                article_title = extract_html_title(html)
                excerpt = strip_html(html, cap=500)
        except Exception:
            excerpt = ""

    name = title or article_title or f"HN#{item_id}"
    return ok_result(
        linked,
        title=name,
        text=excerpt or None,
        t0=t0,
        extras={
            "kind": "hn",
            "name": name,
            "description": (
                f"HN: {score} pts, {n_comments} comments, by {author}"
            ),
            "source_url": url,
            "metadata": {
                "hn_id": int(item_id),
                "hn_score": score,
                "hn_comments": n_comments,
                "hn_author": author,
                "article_title": article_title or None,
            },
        },
    )


__all__ = ["fetch_item"]
