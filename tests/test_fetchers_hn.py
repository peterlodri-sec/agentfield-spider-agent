"""HN Firebase fetcher.

Tests both paths: linked-URL-is-github (recurses into github fetcher
and surfaces the repo) and linked-URL-is-article (fetches HTML +
extracts excerpt). Also Ask/Show HN with selftext.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from spider.fetchers import hn


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        text: str | None = None,
        *,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (
            json.dumps(body) if body is not None else ""
        )
        self.headers = {"Content-Type": content_type}

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
        # routes: list of (url-substring, response). Iterated in order.
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


# ─── HN -> article ──────────────────────────────────────────────────
def test_hn_with_article_link(monkeypatch) -> None:
    _patch_httpx(monkeypatch, [
        ("/v0/item/12345.json", _FakeResponse(200, {
            "title": "Show HN: My cool thing",
            "url": "https://example.com/post",
            "by": "alice",
            "score": 250,
            "descendants": 42,
        })),
        ("example.com/post", _FakeResponse(
            200,
            text="<html><head><title>My cool thing</title></head>"
                 "<body><p>Hello world</p></body></html>",
            content_type="text/html",
        )),
    ])

    out = _run(hn.fetch_item("https://news.ycombinator.com/item?id=12345"))
    assert out["status"] == "ok"
    assert out["kind"] == "hn"
    assert out["url"] == "https://example.com/post"
    assert out["source_url"] == "https://news.ycombinator.com/item?id=12345"
    assert "My cool thing" in out["title"]
    assert "250 pts" in out["description"]
    assert "by alice" in out["description"]
    assert "My cool thing" in (out["text"] or "")
    assert out["metadata"]["hn_score"] == 250
    assert out["metadata"]["hn_comments"] == 42


# ─── HN -> github repo (recursion) ──────────────────────────────────
def test_hn_recurses_into_github(monkeypatch) -> None:
    readme_b64 = base64.b64encode(
        b"# semble\n\nFast and accurate code search.\n",
    ).decode("ascii")
    _patch_httpx(monkeypatch, [
        ("/v0/item/99999.json", _FakeResponse(200, {
            "title": "Show HN: Semble — code search",
            "url": "https://github.com/MinishLab/semble",
            "by": "bibabomas",
            "score": 425,
            "descendants": 137,
        })),
        ("/repos/MinishLab/semble/readme", _FakeResponse(200, {
            "content": readme_b64,
            "encoding": "base64",
        })),
        ("/repos/MinishLab/semble", _FakeResponse(200, {
            "description": "Fast and accurate code search for agents",
            "stargazers_count": 4500,
        })),
    ])

    out = _run(hn.fetch_item("https://news.ycombinator.com/item?id=99999"))
    assert out["status"] == "ok"
    assert out["kind"] == "github-via-hn"
    assert out["url"] == "https://github.com/MinishLab/semble"
    assert out["source_url"] == "https://news.ycombinator.com/item?id=99999"
    assert "Show HN: Semble" in out["description"]
    assert "425 pts" in out["description"]
    assert "Fast and accurate" in out["description"]
    assert "Fast and accurate" in (out["text"] or "")
    # HN metadata is merged with github metadata.
    assert out["metadata"]["hn_score"] == 425
    assert out["metadata"]["stars"] == 4500


# ─── HN Ask/Show with selftext ──────────────────────────────────────
def test_hn_ask_with_selftext_no_url(monkeypatch) -> None:
    _patch_httpx(monkeypatch, [
        ("/v0/item/77777.json", _FakeResponse(200, {
            "title": "Ask HN: What's your stack?",
            "by": "alice",
            "score": 30,
            "descendants": 50,
            "text": "<p>I&#x27;m curious about everyone's tech stack.</p>",
        })),
    ])
    out = _run(hn.fetch_item("https://news.ycombinator.com/item?id=77777"))
    assert out["status"] == "ok"
    assert out["kind"] == "hn"
    assert "What's your stack" in out["title"]
    assert "I'" in (out["text"] or "") or "curious" in (out["text"] or "")
    assert out["metadata"]["hn_score"] == 30


# ─── error cases ─────────────────────────────────────────────────────
def test_hn_no_id_in_url() -> None:
    out = _run(hn.fetch_item("https://news.ycombinator.com/"))
    assert out["status"] == "error"
    assert "no id" in out["error"]


def test_hn_api_4xx(monkeypatch) -> None:
    _patch_httpx(monkeypatch, [
        ("/v0/item/12345.json", _FakeResponse(429)),
    ])
    out = _run(hn.fetch_item("https://news.ycombinator.com/item?id=12345"))
    assert out["status"] == "error"
    assert "hn api" in out["error"]
