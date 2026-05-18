"""GitHub repo + issue fetchers — REST API via httpx.

Why API not browser:
- The HTML for `github.com/{owner}/{repo}` is ~3 MB and JS-heavy; the
  rendered DOM gives you the same description + README content the API
  gives in one ~10 KB JSON response.
- The README endpoint returns the actual `README.md` body (base64
  encoded), not the rendered HTML — better for extracting the first
  prose paragraph.
- Rate limits are higher with GITHUB_TOKEN; unauthenticated is 60/hr.

If GITHUB_TOKEN isn't set we send unauthenticated requests anyway; the
caller will hit 403 when rate-limited and we surface that as
``status="error"`` per the dispatcher contract.
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Any

import httpx

from ..log import log
from ..proxy import httpx_client_kwargs
from .base import (
    base_result,
    error_result,
    first_meaningful_paragraph,
    ok_result,
)

_API_BASE = "https://api.github.com"
_REPO_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?(?:[?#].*)?$", re.I,
)
_ISSUE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", re.I,
)


def _auth_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _gh_get(client: httpx.AsyncClient, path: str) -> dict | None:
    """``GET {API_BASE}{path}``. Returns parsed JSON or ``None`` on 4xx/5xx.

    Caller is expected to interpret a None as "no data; check the
    result.error field". We log the status + path so operators can
    grep for rate-limit / 404 spikes.
    """
    try:
        resp = await client.get(_API_BASE + path, headers=_auth_headers())
    except httpx.RequestError as e:
        log("github.api", state="transport-error", path=path, msg=str(e))
        return None
    if resp.status_code >= 400:
        log(
            "github.api", state="non-2xx",
            path=path, code=resp.status_code,
        )
        return None
    try:
        return resp.json()
    except ValueError:
        log("github.api", state="non-json", path=path)
        return None


async def fetch_repo(url: str, *, proxy: str | None = None) -> dict[str, Any]:
    """Fetch repo metadata + README for ``url``.

    Returns the dispatcher-standard dict with these extras:
      - ``kind`` = ``"github-repo"``
      - ``name`` = ``"{owner}/{repo}"``
      - ``description`` = repo's API description (or first 8 topics)
      - ``title`` = same as name (for callers that look at ``title``)
      - ``text`` = first meaningful README paragraph
      - ``metadata`` = ``{stars, forks, language, license, default_branch, ...}``
    """
    m = _REPO_RE.match(url)
    if not m:
        t0 = time.perf_counter()
        return error_result(url, error="not a github repo url", t0=t0)
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    canonical = f"https://github.com/{owner}/{repo}"
    t0 = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=20.0,
        **httpx_client_kwargs(proxy),
    ) as client:
        repo_json = await _gh_get(client, f"/repos/{owner}/{repo}")
        if repo_json is None:
            return error_result(
                canonical,
                error="github repo api failed (rate-limit, 404, or transport)",
                t0=t0,
                extras={"kind": "github-repo", "name": f"{owner}/{repo}"},
            )
        readme_json = await _gh_get(client, f"/repos/{owner}/{repo}/readme")

    description = (repo_json.get("description") or "").strip()
    if not description:
        topics = repo_json.get("topics") or []
        if topics:
            description = "topics: " + ", ".join(topics[:8])

    text = ""
    if readme_json is not None:
        content_b64 = (readme_json.get("content") or "").replace("\n", "")
        try:
            decoded = base64.b64decode(content_b64).decode(
                "utf-8", errors="replace",
            )
        except Exception:
            decoded = ""
        if decoded:
            text = first_meaningful_paragraph(decoded)

    metadata = {
        "stars": repo_json.get("stargazers_count"),
        "forks": repo_json.get("forks_count"),
        "language": repo_json.get("language"),
        "default_branch": repo_json.get("default_branch"),
        "license": (
            (repo_json.get("license") or {}).get("spdx_id")
            if isinstance(repo_json.get("license"), dict)
            else None
        ),
        "is_archived": bool(repo_json.get("archived")),
        "open_issues": repo_json.get("open_issues_count"),
    }

    return ok_result(
        canonical,
        title=f"{owner}/{repo}",
        text=text or None,
        t0=t0,
        extras={
            "kind": "github-repo",
            "name": f"{owner}/{repo}",
            "description": description or None,
            "metadata": metadata,
        },
    )


async def fetch_issue(url: str, *, proxy: str | None = None) -> dict[str, Any]:
    """Fetch a GitHub issue's title + body + state + labels.

    Extras:
      - ``kind`` = ``"github-issue"``
      - ``name`` = ``"{owner}/{repo}#{n}: {title}"``
      - ``description`` = ``"GitHub issue ({state}); labels: ..."``
      - ``text`` = first paragraph of issue body
      - ``metadata`` = ``{state, labels, user, comments}``
    """
    m = _ISSUE_RE.match(url)
    if not m:
        t0 = time.perf_counter()
        return error_result(url, error="not a github issue url", t0=t0)
    owner, repo, num = m.group(1), m.group(2), m.group(3)
    t0 = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=15.0,
        **httpx_client_kwargs(proxy),
    ) as client:
        issue_json = await _gh_get(
            client, f"/repos/{owner}/{repo}/issues/{num}",
        )

    if issue_json is None:
        return error_result(
            url,
            error="github issue api failed",
            t0=t0,
            extras={"kind": "github-issue", "name": f"{owner}/{repo}#{num}"},
        )

    title = (issue_json.get("title") or "").strip()
    state = issue_json.get("state") or "?"
    labels = [
        (l or {}).get("name", "") for l in (issue_json.get("labels") or [])
    ]
    body = (issue_json.get("body") or "").strip()
    body_excerpt = first_meaningful_paragraph(body) if body else ""

    description = f"GitHub issue ({state})"
    if labels:
        description += f", labels: {', '.join(labels)}"

    metadata = {
        "state": state,
        "labels": labels,
        "user": (issue_json.get("user") or {}).get("login"),
        "comments": issue_json.get("comments"),
    }

    return ok_result(
        url,
        title=f"{owner}/{repo}#{num}: {title}" if title else f"{owner}/{repo}#{num}",
        text=body_excerpt or None,
        t0=t0,
        extras={
            "kind": "github-issue",
            "name": f"{owner}/{repo}#{num}",
            "description": description,
            "metadata": metadata,
        },
    )


__all__ = ["fetch_issue", "fetch_repo"]
