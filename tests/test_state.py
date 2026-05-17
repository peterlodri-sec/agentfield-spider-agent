"""Redis state helpers — verified against fakeredis."""
from __future__ import annotations

import asyncio
from typing import Any

import orjson
import pytest

from spider import state


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_url_hash_is_stable_and_unique() -> None:
    a = state.url_hash("https://example.com/")
    b = state.url_hash("https://example.com/")
    c = state.url_hash("https://example.com/foo")
    assert a == b
    assert a != c
    assert len(a) == 40  # sha1 hex


def test_enqueue_and_queue_depth(fake_redis) -> None:
    async def _go():
        assert await state.queue_depth(fake_redis) == 0
        await state.enqueue(fake_redis, "https://a.example/", source="api")
        await state.enqueue(fake_redis, "https://b.example/", source="api")
        assert await state.queue_depth(fake_redis) == 2

    _run(_go())


def test_mark_seen_and_is_seen(fake_redis) -> None:
    async def _go():
        u = "https://example.com/foo"
        assert await state.is_seen(fake_redis, u) is False
        await state.mark_seen(fake_redis, u)
        assert await state.is_seen(fake_redis, u) is True
        assert await state.seen_count(fake_redis) == 1

    _run(_go())


def test_record_job_round_trip(fake_redis) -> None:
    async def _go():
        job = {
            "id": "abc123",
            "url": "https://example.com/",
            "status": "ok",
            "ms": 42,
            "ts": 1_700_000_000_000,
        }
        await state.record_job(fake_redis, job)
        assert await state.jobs_count(fake_redis) == 1
        recent = await state.recent_jobs(fake_redis, limit=10)
        assert recent and recent[0]["id"] == "abc123"
        assert recent[0]["status"] == "ok"

    _run(_go())


def test_recent_jobs_sorts_newest_first(fake_redis) -> None:
    async def _go():
        for i in range(3):
            await state.record_job(fake_redis, {
                "id": f"j{i}",
                "url": "https://example.com/",
                "status": "ok",
                "ms": 1,
                "ts": 1_700_000_000_000 + i,
            })
        recent = await state.recent_jobs(fake_redis, limit=10)
        assert [j["id"] for j in recent] == ["j2", "j1", "j0"]

    _run(_go())


def test_recent_jobs_limit_clamps_to_bounds(fake_redis) -> None:
    async def _go():
        for i in range(5):
            await state.record_job(fake_redis, {
                "id": f"j{i}", "url": "u", "status": "ok",
                "ms": 1, "ts": i + 1,
            })
        # 0 -> 1 (clamped to minimum)
        assert len(await state.recent_jobs(fake_redis, limit=0)) == 1
        # 1000 -> 500 (clamped to max)
        assert len(await state.recent_jobs(fake_redis, limit=1000)) == 5
