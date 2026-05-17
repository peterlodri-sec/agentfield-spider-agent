"""Cover the host-of helper, the RobotsCache TTL behaviour, and the
HostBucket's per-host throttling.

RobotsCache is exercised against a fake aiohttp-like session so we
don't make network requests in CI.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from spider.politeness import HostBucket, RobotsCache, host_of


def test_host_of_handles_basic_urls() -> None:
    assert host_of("https://example.com/foo") == "example.com"
    assert host_of("http://example.com:8080/x") == "example.com:8080"
    assert host_of("not-a-url") == ""


# ─── RobotsCache ────────────────────────────────────────────────────
class _FakeRobotsResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_FakeRobotsResponse":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def text(self) -> str:
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeRobotsResponse) -> None:
        self._response = response
        self.get_calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _FakeRobotsResponse:
        self.get_calls.append(url)
        return self._response


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_robots_cache_blocks_disallowed_path() -> None:
    body = "User-agent: *\nDisallow: /private/"
    sess = _FakeSession(_FakeRobotsResponse(200, body))
    rc = RobotsCache(sess)  # type: ignore[arg-type]
    assert _run(rc.allowed("https://example.com/public")) is True
    assert _run(rc.allowed("https://example.com/private/foo")) is False
    # Second call for the same host must NOT re-fetch (TTL window).
    assert _run(rc.allowed("https://example.com/other")) is True
    assert sess.get_calls == ["https://example.com/robots.txt"]


def test_robots_cache_4xx_treated_as_no_policy() -> None:
    sess = _FakeSession(_FakeRobotsResponse(404))
    rc = RobotsCache(sess)  # type: ignore[arg-type]
    assert _run(rc.allowed("https://example.com/anything")) is True


# ─── HostBucket ─────────────────────────────────────────────────────
def test_host_bucket_first_token_is_free() -> None:
    bucket = HostBucket()
    t0 = time.monotonic()
    _run(bucket.acquire("example.com"))
    assert (time.monotonic() - t0) < 0.05


def test_host_bucket_empty_host_is_noop() -> None:
    bucket = HostBucket()
    t0 = time.monotonic()
    _run(bucket.acquire(""))
    assert (time.monotonic() - t0) < 0.05
