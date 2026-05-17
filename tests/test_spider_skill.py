"""Tests for the reasoner-layer wrappers ``_fetch`` and ``_feed_add``.

These exercise the HTTP-proxy logic without main.py / Agent
construction. The httpx client is replaced with a small fake that
records calls + returns canned responses, so no real socket is
opened.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from reasoners import _spider


class _FakeAsyncResponse:
    def __init__(
        self,
        status_code: int,
        body: dict | None = None,
        *,
        raise_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self._raise_json = raise_json
        self.text = (
            "<not-json>" if raise_json else json.dumps(self._body)
        )

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class _FakeAsyncClient:
    """Replaces ``httpx.AsyncClient`` for the duration of a test.

    Records every call into ``calls`` and returns the canned response
    queue in order. If only one response is queued, every call returns
    it (handy for "always succeed" tests).
    """

    def __init__(self, responses: list[_FakeAsyncResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, json: dict | None = None, **kwargs: Any) -> _FakeAsyncResponse:
        self.calls.append((url, json or {}))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _patch_client(monkeypatch, responses: list[_FakeAsyncResponse]) -> _FakeAsyncClient:
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr(_spider, "_client_factory", lambda: fake)
    return fake


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── _fetch ─────────────────────────────────────────────────────────
def test_fetch_sync_posts_to_fetch_sync_and_returns_body(monkeypatch) -> None:
    payload = {
        "status": "ok",
        "id": "abc",
        "url": "https://example.com/",
        "title": "Example",
        "text": "hello",
        "ms": 12,
    }
    fake = _patch_client(monkeypatch, [_FakeAsyncResponse(200, payload)])
    result = _run(_spider._fetch("https://example.com/"))
    assert result["status"] == "ok"
    assert result["title"] == "Example"
    assert fake.calls and fake.calls[0][0].endswith("/fetch/sync")
    assert fake.calls[0][1] == {"url": "https://example.com/"}


def test_fetch_async_posts_to_fetch_path(monkeypatch) -> None:
    fake = _patch_client(
        monkeypatch, [_FakeAsyncResponse(200, {"queued": True, "url": "https://x"})],
    )
    result = _run(_spider._fetch("https://x", sync=False))
    assert result == {"queued": True, "url": "https://x"}
    assert fake.calls[0][0].endswith("/fetch")


def test_fetch_handles_204_as_empty(monkeypatch) -> None:
    _patch_client(monkeypatch, [_FakeAsyncResponse(204, {})])
    result = _run(_spider._fetch("https://example.com/"))
    assert result == {"status": "empty", "url": "https://example.com/"}


def test_fetch_handles_non_json_response(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        [_FakeAsyncResponse(502, raise_json=True)],
    )
    result = _run(_spider._fetch("https://example.com/"))
    assert result["status"] == "error"
    assert "non-json" in result["error"]


def test_fetch_handles_transport_error(monkeypatch) -> None:
    import httpx

    class _RaisingClient:
        async def __aenter__(self) -> "_RaisingClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(_spider, "_client_factory", lambda: _RaisingClient())
    result = _run(_spider._fetch("https://example.com/"))
    assert result["status"] == "error"
    assert "ConnectError" in result["error"]


# ─── _feed_add ──────────────────────────────────────────────────────
def test_feed_add_posts_with_optional_fields(monkeypatch) -> None:
    payload = {
        "id": "abcdef01",
        "url": "https://feed.example/rss",
        "name": "feed",
        "interval_s": 1800,
        "last_polled": 0,
    }
    fake = _patch_client(monkeypatch, [_FakeAsyncResponse(200, payload)])
    result = _run(_spider._feed_add(
        "https://feed.example/rss", interval_s=1800, name="feed",
    ))
    assert result["id"] == "abcdef01"
    body = fake.calls[0][1]
    assert body == {
        "url": "https://feed.example/rss",
        "interval_s": 1800,
        "name": "feed",
    }


def test_feed_add_passes_through_400_error_body(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        [_FakeAsyncResponse(400, {"error": "url failed SSRF gate"})],
    )
    result = _run(_spider._feed_add("http://localhost/feed.xml"))
    assert result["error"] == "url failed SSRF gate"
    assert result.get("status_code") == 400


def test_feed_add_handles_transport_error(monkeypatch) -> None:
    import httpx

    class _RaisingClient:
        async def __aenter__(self) -> "_RaisingClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(_spider, "_client_factory", lambda: _RaisingClient())
    result = _run(_spider._feed_add("https://feed.example/rss"))
    assert result["status"] == "error"
    assert "ReadTimeout" in result["error"]
