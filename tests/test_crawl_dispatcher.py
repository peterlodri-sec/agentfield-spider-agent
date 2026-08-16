"""Tests for crawl background dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from spider.crawl import _active_crawl_job_ids, crawl_dispatcher


@pytest.mark.asyncio
async def test_active_crawl_job_ids_from_scan():
    redis = AsyncMock()

    async def fake_scan(cursor: int, *, match: str, count: int) -> tuple[int, list[bytes]]:
        assert match == "crawler:crawl:*:state"
        if cursor == 0:
            return (
                0,
                [
                    b"crawler:crawl:job-a:state",
                    b"crawler:crawl:job-b:state",
                ],
            )
        return (0, [])

    redis.scan = fake_scan

    assert await _active_crawl_job_ids(redis) == ["job-a", "job-b"]


@pytest.mark.asyncio
async def test_crawl_dispatcher_invokes_iter_for_active_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AsyncMock()
    iter_calls: list[str] = []

    async def fake_scan(cursor: int, *, match: str, count: int) -> tuple[int, list[bytes]]:
        if cursor == 0:
            return (0, [b"crawler:crawl:abc123:state"])
        return (0, [])

    redis.scan = fake_scan

    async def fake_status(_redis: object, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "status": "queued"}

    async def fake_iter(_deps: object, _redis: object, job_id: str) -> dict[str, object]:
        iter_calls.append(job_id)
        return {"status": "running"}

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("spider.crawl.crawl_status", fake_status)
    monkeypatch.setattr("spider.crawl.crawl_iter", fake_iter)
    monkeypatch.setattr("spider.crawl.asyncio.sleep", fake_sleep)

    deps = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await crawl_dispatcher(deps, redis)

    assert iter_calls == ["abc123"]


@pytest.mark.asyncio
async def test_crawl_dispatcher_skips_terminal_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AsyncMock()
    iter_calls: list[str] = []

    async def fake_scan(cursor: int, *, match: str, count: int) -> tuple[int, list[bytes]]:
        if cursor == 0:
            return (0, [b"crawler:crawl:done-job:state"])
        return (0, [])

    redis.scan = fake_scan

    async def fake_status(_redis: object, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "status": "completed"}

    async def fake_iter(_deps: object, _redis: object, job_id: str) -> dict[str, object]:
        iter_calls.append(job_id)
        return {}

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("spider.crawl.crawl_status", fake_status)
    monkeypatch.setattr("spider.crawl.crawl_iter", fake_iter)
    monkeypatch.setattr("spider.crawl.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await crawl_dispatcher(MagicMock(), redis)

    assert iter_calls == []
