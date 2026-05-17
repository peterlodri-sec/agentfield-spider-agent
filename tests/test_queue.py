"""Dispatcher-worker helpers.

The dispatcher loop itself runs forever (BRPOP-blocked), so we exercise
the ``run_and_record`` and ``_run_one`` helpers directly. End-to-end
loop coverage lives in the docker compose smoke ladder.
"""
from __future__ import annotations

import asyncio
from typing import Any

import orjson
import pytest

from spider import queue, state


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_run_and_record_writes_to_redis(make_deps, monkeypatch, fake_redis) -> None:
    """A successful fetch SADDs to seen and ZADDs a job record."""
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(sess: Any, url: str, **kw: Any) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(fetch, "is_safe_external_url_with_dns", _allow_all)
    monkeypatch.setattr(fetch, "follow_safe_redirects", _identity_redirect)

    deps, page = make_deps()

    async def _go():
        result = await queue.run_and_record(
            deps, fake_redis, "https://example.com/", source="api-sync",
        )
        assert result["status"] == "ok"
        assert result["source"] == "api-sync"
        assert page.goto_calls[0][0] == "https://example.com/"
        assert await state.is_seen(fake_redis, "https://example.com/") is True
        recent = await state.recent_jobs(fake_redis, limit=10)
        assert recent and recent[0]["status"] == "ok"

    _run(_go())


def test_run_and_record_records_failure_without_marking_seen(
    make_deps, make_page, monkeypatch, fake_redis,
) -> None:
    """A browser-error fetch records the job but does NOT mark seen."""
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(sess: Any, url: str, **kw: Any) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(fetch, "is_safe_external_url_with_dns", _allow_all)
    monkeypatch.setattr(fetch, "follow_safe_redirects", _identity_redirect)

    page = make_page(goto_should_fail=True)
    deps, _ = make_deps(page)

    async def _go():
        result = await queue.run_and_record(
            deps, fake_redis, "https://example.com/", source="api-sync",
        )
        assert result["status"] == "error"
        assert await state.is_seen(fake_redis, "https://example.com/") is False
        recent = await state.recent_jobs(fake_redis, limit=10)
        assert recent and recent[0]["status"] == "error"

    _run(_go())


def test_run_and_record_handles_missing_redis(make_deps, monkeypatch) -> None:
    """Passing redis=None makes recording a no-op (still returns result)."""
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(sess: Any, url: str, **kw: Any) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(fetch, "is_safe_external_url_with_dns", _allow_all)
    monkeypatch.setattr(fetch, "follow_safe_redirects", _identity_redirect)

    deps, _ = make_deps()

    async def _go():
        result = await queue.run_and_record(
            deps, None, "https://example.com/", source="manual",
        )
        assert result["status"] == "ok"

    _run(_go())


def test_run_one_decodes_job_payload(
    make_deps, monkeypatch, fake_redis,
) -> None:
    """_run_one parses the orjson-encoded queue entry and forwards source."""
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(sess: Any, url: str, **kw: Any) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(fetch, "is_safe_external_url_with_dns", _allow_all)
    monkeypatch.setattr(fetch, "follow_safe_redirects", _identity_redirect)

    deps, page = make_deps()
    sem = asyncio.Semaphore(1)

    async def _go():
        await sem.acquire()
        raw = orjson.dumps({"url": "https://example.com/x", "source": "feed:abc"})
        await queue._run_one(deps, fake_redis, raw, sem)  # noqa: SLF001
        assert page.goto_calls[0][0] == "https://example.com/x"
        recent = await state.recent_jobs(fake_redis, limit=10)
        assert recent and recent[0]["source"] == "feed:abc"

    _run(_go())


def test_run_one_gracefully_ignores_malformed_payload(
    make_deps, fake_redis,
) -> None:
    deps, _ = make_deps()
    sem = asyncio.Semaphore(1)

    async def _go():
        await sem.acquire()
        await queue._run_one(deps, fake_redis, b"not-json", sem)  # noqa: SLF001
        # No exception, no job recorded.
        assert await state.jobs_count(fake_redis) == 0

    _run(_go())
