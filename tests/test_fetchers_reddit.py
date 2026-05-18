"""Reddit `.json` fetcher.

Tests the redirect-follow, tracking-param-strip, JSON parse path.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from spider.fetchers import reddit


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers: dict[str, str] = {}
        # httpx-like .url attribute (real httpx returns a URL object).
        self.url = url or ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )


class _FakeClient:
    def __init__(self, routes: list[tuple[str, _FakeResponse]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append((url, kw))
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError(f"no route for url: {url}")


def _patch_httpx(monkeypatch, routes: list[tuple[str, _FakeResponse]]) -> _FakeClient:
    fake = _FakeClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


_THREAD_PAYLOAD = [{
    "data": {
        "children": [{
            "data": {
                "title": "The golden era of software engineering is now",
                "subreddit": "AgentsOfAI",
                "author": "orbny",
                "score": 89,
                "num_comments": 34,
                "selftext": "",
                "url": "https://i.redd.it/abc.jpeg",
                "is_self": False,
            },
        }],
    },
}]


def test_reddit_thread_link_post(monkeypatch) -> None:
    # 1st call: redirect from shortlink resolves to the canonical URL
    #            (with tracking params — those have to get stripped).
    # 2nd call: JSON endpoint at the stripped canonical URL.
    redirect_target = (
        "https://www.reddit.com/r/AgentsOfAI/comments/1tg1ear/"
        "the_golden_era_of_software_engineering_is_now/"
        "?utm_source=share&share_id=NOISE"
    )
    _patch_httpx(monkeypatch, [
        # The shortlink fetch returns a "200" — we use the final URL
        # via the .url field (set by follow_redirects=True in real httpx).
        ("/s/CUBmeH7fPl", _FakeResponse(
            200, body={}, url=redirect_target,
        )),
        (".json", _FakeResponse(200, body=_THREAD_PAYLOAD)),
    ])

    out = _run(reddit.fetch_thread(
        "https://www.reddit.com/r/AgentsOfAI/s/CUBmeH7fPl",
    ))

    assert out["status"] == "ok"
    assert out["kind"] == "reddit"
    assert "golden era" in out["title"]
    assert out["url"].endswith("/")  # canonical URL kept its trailing slash
    assert "?" not in out["url"]      # tracking params stripped
    assert out["source_url"] == "https://www.reddit.com/r/AgentsOfAI/s/CUBmeH7fPl"
    assert "r/AgentsOfAI" in out["description"]
    assert "89 pts" in out["description"]
    assert "u/orbny" in out["description"]
    assert out["text"] is not None
    assert "link post" in out["text"]
    assert out["metadata"]["subreddit"] == "AgentsOfAI"
    assert out["metadata"]["link_url"].startswith("https://i.redd.it")


def test_reddit_thread_with_selftext(monkeypatch) -> None:
    payload = [{
        "data": {
            "children": [{
                "data": {
                    "title": "Self post",
                    "subreddit": "python",
                    "author": "alice",
                    "score": 10,
                    "num_comments": 5,
                    "selftext": "Long thoughtful body here.",
                    "url": "https://reddit.com/...",
                    "is_self": True,
                },
            }],
        },
    }]
    _patch_httpx(monkeypatch, [
        ("/r/python/comments/abc/post/", _FakeResponse(
            200, body={}, url="https://www.reddit.com/r/python/comments/abc/post/",
        )),
        (".json", _FakeResponse(200, body=payload)),
    ])
    out = _run(reddit.fetch_thread(
        "https://www.reddit.com/r/python/comments/abc/post/",
    ))
    assert out["status"] == "ok"
    assert out["text"] == "Long thoughtful body here."
    assert out["metadata"]["is_self"] is True


def test_reddit_json_endpoint_returns_html(monkeypatch) -> None:
    """Reddit serves HTML to non-browser UAs; the fetcher surfaces this
    as an error so the caller knows to retry with the browser path.
    Simulated via a 200 with non-JSON body (httpx would raise ValueError
    when .json() is called)."""
    _patch_httpx(monkeypatch, [
        ("/r/", _FakeResponse(200, body={}, url="https://www.reddit.com/r/x/comments/y/z/")),
        (".json", _FakeResponse(200, body=None)),  # no body => ValueError on .json()
    ])
    out = _run(reddit.fetch_thread("https://www.reddit.com/r/x/comments/y/z/"))
    assert out["status"] == "error"
    assert "reddit json" in out["error"]


def test_reddit_transport_error(monkeypatch) -> None:
    class _RaisingClient:
        async def __aenter__(self) -> "_RaisingClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:  # noqa: ARG002
            raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _RaisingClient())
    out = _run(reddit.fetch_thread("https://www.reddit.com/r/x/s/abc"))
    assert out["status"] == "error"
    assert "ConnectError" in out["error"]
