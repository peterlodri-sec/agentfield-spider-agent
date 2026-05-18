"""SessionManager unit tests.

Exercises the manager against a fake pool that mimics the
``_browser.new_context()`` / ``BrowserContext.new_page()`` /
``Page.close`` chain. No real browser, no playwright.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spider.sessions import (
    MAX_SESSIONS,
    SESSION_TTL_S,
    SessionInfo,
    SessionManager,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── fakes that mimic playwright shapes ─────────────────────────────
class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False
        self.kwargs: dict[str, Any] = {}
        self._storage_state: dict[str, Any] = {"cookies": [], "origins": []}

    async def storage_state(self) -> dict[str, Any]:
        return dict(self._storage_state)

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        ctx = _FakeContext()
        ctx.kwargs = kwargs
        self.contexts.append(ctx)
        return ctx


class _FakePool:
    """Mimics CloakBrowserPool / LightpandaPool just enough for SessionManager.

    SessionManager calls ``_get_browser(pool)`` which checks
    ``pool._browser`` and falls back to ``await pool.lease()``. We set
    ``_browser`` directly so no lease is needed.
    """

    def __init__(self) -> None:
        self._browser = _FakeBrowser()


# ─── basics ──────────────────────────────────────────────────────────
def test_create_session_returns_info() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            name="alice",
            metadata={"user_id": "abc"},
        )

    info = _run(_go())
    assert isinstance(info, SessionInfo)
    assert info.name == "alice"
    assert info.metadata == {"user_id": "abc"}
    assert info.id and len(info.id) == 12
    assert info.created_at > 0
    assert info.last_used == info.created_at
    assert info.n_pages_opened == 0
    # The pool got a new_context() call.
    assert len(pool._browser.contexts) == 1


def test_create_session_applies_storage_state() -> None:
    pool = _FakePool()
    mgr = SessionManager()
    ss = {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}

    async def _go():
        return await mgr.create(pool=pool, storage_state=ss)

    _run(_go())
    ctx = pool._browser.contexts[0]
    assert ctx.kwargs.get("storage_state") == ss


def test_export_storage_state_before_close() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        info = await mgr.create(pool=pool)
        state = await mgr.export_storage_state(info.id)
        await mgr.close(info.id)
        return state

    state = _run(_go())
    assert state == {"cookies": [], "origins": []}


def test_create_session_forwards_ua_and_viewport() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            user_agent="Mozilla/5.0 (test)",
            viewport={"width": 1280, "height": 720},
        )

    _run(_go())
    ctx = pool._browser.contexts[0]
    assert ctx.kwargs["user_agent"] == "Mozilla/5.0 (test)"
    assert ctx.kwargs["viewport"] == {"width": 1280, "height": 720}


def test_get_returns_info_after_create() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        info = await mgr.create(pool=pool, name="test")
        return await mgr.get(info.id), info

    fetched, original = _run(_go())
    assert fetched == original
    assert fetched.name == "test"


def test_get_returns_none_for_unknown_id() -> None:
    mgr = SessionManager()

    async def _go():
        return await mgr.get("nope")

    assert _run(_go()) is None


def test_list_newest_first() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        # Create three sessions; sleep long enough that timestamps
        # differ at ms resolution on any CI runner. 50ms is well above
        # any sleep-precision floor.
        a = await mgr.create(pool=pool, name="a")
        await asyncio.sleep(0.05)
        b = await mgr.create(pool=pool, name="b")
        await asyncio.sleep(0.05)
        c = await mgr.create(pool=pool, name="c")
        return a, b, c

    a, b, c = _run(_go())
    listed = _run(mgr.list())
    assert [s.name for s in listed] == ["c", "b", "a"]


# ─── close / close_all ──────────────────────────────────────────────
def test_close_returns_true_and_closes_context() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        info = await mgr.create(pool=pool, name="x")
        return await mgr.close(info.id)

    assert _run(_go()) is True
    # The fake context's close was invoked.
    assert pool._browser.contexts[0].closed is True


def test_close_returns_false_for_unknown() -> None:
    mgr = SessionManager()

    async def _go():
        return await mgr.close("nope")

    assert _run(_go()) is False


def test_close_all_closes_all_contexts() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        await mgr.create(pool=pool, name="a")
        await mgr.create(pool=pool, name="b")
        await mgr.close_all()
        return await mgr.list()

    remaining = _run(_go())
    assert remaining == []
    assert all(c.closed for c in pool._browser.contexts)


# ─── lease_page ──────────────────────────────────────────────────────
def test_lease_page_yields_page_on_named_context() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        info = await mgr.create(pool=pool, name="alice")
        async with mgr.lease_page(info.id, pool=pool) as page:
            assert isinstance(page, _FakePage)
            assert page.closed is False
        # Page closed on exit; session info updated.
        return info.id

    sid = _run(_go())
    updated = _run(mgr.get(sid))
    assert updated.n_pages_opened == 1
    # The lease's page is closed; the session's context is not.
    ctx = pool._browser.contexts[0]
    assert ctx.closed is False
    assert ctx.pages[0].closed is True


def test_lease_page_raises_keyerror_for_unknown_session() -> None:
    mgr = SessionManager()
    pool = _FakePool()

    async def _go():
        async with mgr.lease_page("nope", pool=pool):
            pass

    with pytest.raises(KeyError):
        _run(_go())


def test_lease_page_raises_when_pool_mismatch() -> None:
    pool_a = _FakePool()
    pool_b = _FakePool()
    mgr = SessionManager()

    async def _go():
        info = await mgr.create(pool=pool_a, name="x")
        async with mgr.lease_page(info.id, pool=pool_b):
            pass

    with pytest.raises(RuntimeError, match="different pool"):
        _run(_go())


# ─── TTL ─────────────────────────────────────────────────────────────
def test_lease_page_expires_old_sessions(monkeypatch) -> None:
    pool = _FakePool()
    # Very short TTL so we can age out a session deterministically.
    mgr = SessionManager(ttl_s=1)

    async def _go():
        info = await mgr.create(pool=pool, name="ages-fast")
        # Forge the last_used to 2 seconds ago.
        s_internal = mgr._sessions[info.id]
        s_internal.info.last_used = s_internal.info.last_used - 5000
        # Now leasing should raise KeyError (expired) AND remove it.
        try:
            async with mgr.lease_page(info.id, pool=pool):
                pass
        except KeyError:
            return await mgr.get(info.id)
        return "no-error"

    out = _run(_go())
    assert out is None  # expired session removed


# ─── eviction (LRU at hard cap) ─────────────────────────────────────
def test_eviction_kicks_lru_when_over_cap() -> None:
    pool = _FakePool()
    mgr = SessionManager(max_sessions=2)

    async def _go():
        a = await mgr.create(pool=pool, name="a")
        await asyncio.sleep(0.05)
        b = await mgr.create(pool=pool, name="b")
        await asyncio.sleep(0.05)
        # Touch `a` so it's most-recently-used (now > b).
        async with mgr.lease_page(a.id, pool=pool):
            pass
        await asyncio.sleep(0.05)
        # Adding c should evict `b` (now the LRU).
        c = await mgr.create(pool=pool, name="c")
        return a.id, b.id, c.id

    a_id, b_id, c_id = _run(_go())
    listed_ids = {s.id for s in _run(mgr.list())}
    assert listed_ids == {a_id, c_id}, f"expected b ({b_id}) evicted; got {listed_ids}"


# ─── env defaults ────────────────────────────────────────────────────
def test_env_defaults_are_sensible() -> None:
    """Defensive: ensure env-derived defaults stay reasonable."""
    assert MAX_SESSIONS >= 1
    assert SESSION_TTL_S >= 60  # 1 minute floor
