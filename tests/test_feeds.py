"""Feed CRUD + poll_feed behaviour.

Uses fakeredis for storage and a small fake aiohttp session that
returns a canned RSS body. ``is_safe_external_url_with_dns`` is
monkeypatched so we don't hit DNS in CI.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson
import pytest

from spider import feeds, state


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_RSS_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel>'
    b'<title>Test</title><link>https://feed.example/</link>'
    b'<description>t</description>'
    b'<item>'
    b'<title>A</title><link>https://a.example/post-1</link>'
    b'</item>'
    b'<item>'
    b'<title>B</title><link>https://b.example/post-2</link>'
    b'</item>'
    b'</channel></rss>'
)


class _FakeResp:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    class _Content:
        def __init__(self, body: bytes) -> None:
            self._body = body

        async def read(self, n: int) -> bytes:
            return self._body[:n]

    async def __aenter__(self) -> "_FakeResp":
        self.content = self._Content(self._body)  # type: ignore[attr-defined]
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeHttp:
    def __init__(self, response: _FakeResp) -> None:
        self._response = response
        self.get_calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.get_calls.append((url, kwargs))
        return self._response


def test_feed_add_persists_and_lists(fake_redis) -> None:
    async def _go():
        feed = await feeds.feed_add(
            fake_redis,
            url="https://feed.example/rss",
            name="my feed",
            interval_s=7200,
        )
        assert feed["id"]
        assert feed["url"] == "https://feed.example/rss"
        assert feed["name"] == "my feed"
        assert feed["interval_s"] == 7200

        listed = await feeds.feed_list(fake_redis)
        assert len(listed) == 1
        assert listed[0]["id"] == feed["id"]

    _run(_go())


def test_feed_get_and_del(fake_redis) -> None:
    async def _go():
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        got = await feeds.feed_get(fake_redis, feed["id"])
        assert got and got["url"] == "https://feed.example/rss"

        assert await feeds.feed_del(fake_redis, feed["id"]) is True
        assert await feeds.feed_get(fake_redis, feed["id"]) is None
        assert await feeds.feed_del(fake_redis, feed["id"]) is False

    _run(_go())


def test_feed_get_returns_none_for_unknown_id(fake_redis) -> None:
    async def _go():
        assert await feeds.feed_get(fake_redis, "deadbeef") is None

    _run(_go())


def test_poll_feed_enqueues_new_entries(fake_redis, monkeypatch) -> None:
    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(feeds, "is_safe_external_url_with_dns", _allow_all)

    http = _FakeHttp(_FakeResp(200, body=_RSS_BODY, headers={"ETag": '"abc"'}))

    async def _go():
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        result = await feeds.poll_feed(http, fake_redis, feed["id"], feed)  # type: ignore[arg-type]
        assert result["found"] == 2
        assert result["new"] == 2
        assert result["status"] == 200
        # Both URLs landed on the queue.
        assert await state.queue_depth(fake_redis) == 2
        # ETag was persisted.
        refreshed = await feeds.feed_get(fake_redis, feed["id"])
        assert refreshed and refreshed.get("last_etag") == '"abc"'

    _run(_go())


def test_poll_feed_skips_already_seen(fake_redis, monkeypatch) -> None:
    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(feeds, "is_safe_external_url_with_dns", _allow_all)

    http = _FakeHttp(_FakeResp(200, body=_RSS_BODY))

    async def _go():
        # Pre-mark post-1 as seen.
        await state.mark_seen(fake_redis, "https://a.example/post-1")
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        result = await feeds.poll_feed(http, fake_redis, feed["id"], feed)  # type: ignore[arg-type]
        # Only post-2 is new.
        assert result["new"] == 1
        assert await state.queue_depth(fake_redis) == 1

    _run(_go())


def test_poll_feed_handles_304_not_modified(fake_redis, monkeypatch) -> None:
    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(feeds, "is_safe_external_url_with_dns", _allow_all)

    http = _FakeHttp(_FakeResp(304))

    async def _go():
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        feed["last_etag"] = '"prev"'
        result = await feeds.poll_feed(http, fake_redis, feed["id"], feed)  # type: ignore[arg-type]
        assert result["status"] == 304
        assert result["new"] == 0

    _run(_go())


def test_poll_feed_treats_redirect_as_failure(fake_redis, monkeypatch) -> None:
    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(feeds, "is_safe_external_url_with_dns", _allow_all)

    http = _FakeHttp(_FakeResp(301, headers={"Location": "https://elsewhere"}))

    async def _go():
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        result = await feeds.poll_feed(http, fake_redis, feed["id"], feed)  # type: ignore[arg-type]
        assert "error" in result
        assert result["new"] == 0

    _run(_go())


def test_poll_feed_blocks_unsafe_feed_url(fake_redis, monkeypatch) -> None:
    async def _block_all(url: str) -> bool:  # noqa: ARG001
        return False

    monkeypatch.setattr(feeds, "is_safe_external_url_with_dns", _block_all)

    http = _FakeHttp(_FakeResp(200, body=_RSS_BODY))

    async def _go():
        feed = await feeds.feed_add(fake_redis, url="https://feed.example/rss")
        result = await feeds.poll_feed(http, fake_redis, feed["id"], feed)  # type: ignore[arg-type]
        assert "error" in result
        assert "SSRF" in result["error"]
        assert await state.queue_depth(fake_redis) == 0

    _run(_go())
