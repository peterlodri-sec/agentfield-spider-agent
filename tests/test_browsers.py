"""Browser-backend registry + Lightpanda pool tests.

LightpandaPool is exercised via an injected ``connect_factory`` so
the tests never depend on playwright being installed or on a real
Lightpanda CDP server.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spider import browsers
from spider.browsers import (
    DEFAULT_BACKEND,
    env_backend,
    get_or_create_pool,
    make_pool,
    normalize_backend,
    register_primary,
    shutdown_all,
)
from spider.browsers.lightpanda import (
    LightpandaPool,
    MissingPlaywrightError,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── normalize_backend ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "cloak"),
        ("", "cloak"),
        ("cloak", "cloak"),
        ("CLOAK", "cloak"),
        ("  cloak  ", "cloak"),
        ("lightpanda", "lightpanda"),
        ("Lightpanda", "lightpanda"),
        ("puppeteer", "cloak"),  # unknown → default
        ("garbage", "cloak"),
    ],
)
def test_normalize_backend(raw: str | None, expected: str) -> None:
    assert normalize_backend(raw) == expected


def test_env_backend_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "lightpanda")
    assert env_backend() == "lightpanda"


def test_env_backend_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_BACKEND", raising=False)
    assert env_backend() == DEFAULT_BACKEND
    assert env_backend() == "cloak"


def test_env_backend_unknown_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "puppeteer")
    assert env_backend() == "cloak"


# ─── make_pool ───────────────────────────────────────────────────────
def test_make_pool_cloak_returns_cloak_subclass() -> None:
    pool = make_pool("cloak")
    # Don't import the cloak class directly here — just check the name.
    assert "Cloak" in type(pool).__name__


def test_make_pool_lightpanda_returns_lightpanda_pool() -> None:
    pool = make_pool("lightpanda")
    assert isinstance(pool, LightpandaPool)


# ─── registry: get_or_create_pool + register_primary ─────────────────
def test_get_or_create_pool_caches(monkeypatch) -> None:
    # Use fresh registry per test by clearing the module-level cache.
    monkeypatch.setattr(browsers, "_lazy_pools", {})

    async def _go():
        p1 = await get_or_create_pool("lightpanda")
        p2 = await get_or_create_pool("lightpanda")
        return p1, p2

    p1, p2 = _run(_go())
    assert p1 is p2  # same instance returned


def test_register_primary_makes_it_the_cached_instance(monkeypatch) -> None:
    monkeypatch.setattr(browsers, "_lazy_pools", {})

    primary = LightpandaPool()
    register_primary("lightpanda", primary)

    async def _go():
        return await get_or_create_pool("lightpanda")

    fetched = _run(_go())
    assert fetched is primary


def test_shutdown_all_clears_registry(monkeypatch) -> None:
    monkeypatch.setattr(browsers, "_lazy_pools", {})
    _ = _run(get_or_create_pool("lightpanda"))
    assert len(browsers._lazy_pools) == 1
    _run(shutdown_all())
    assert len(browsers._lazy_pools) == 0


# ─── LightpandaPool lease (with injected connect_factory) ────────────
class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = [_FakeContext()]
        self.closed = False

    async def new_context(self) -> _FakeContext:  # pragma: no cover - has 1
        c = _FakeContext()
        self.contexts.append(c)
        return c

    async def close(self) -> None:
        self.closed = True


def test_lightpanda_pool_lease_yields_page() -> None:
    fake_browser = _FakeBrowser()

    async def fake_connect(cdp_url: str) -> _FakeBrowser:
        return fake_browser

    pool = LightpandaPool(connect_factory=fake_connect)

    async def _go():
        async with pool.lease() as page:
            assert isinstance(page, _FakePage)
            assert page.closed is False
        # On exit, page is closed.
        return page

    p = _run(_go())
    assert p.closed is True
    # Reused the existing context.
    assert len(fake_browser.contexts) == 1


def test_lightpanda_pool_shutdown_closes_browser() -> None:
    fake_browser = _FakeBrowser()

    async def fake_connect(cdp_url: str) -> _FakeBrowser:
        return fake_browser

    pool = LightpandaPool(connect_factory=fake_connect)

    async def _go():
        async with pool.lease():
            pass
        await pool.shutdown()

    _run(_go())
    assert fake_browser.closed is True
    assert pool._browser is None


def test_lightpanda_pool_missing_playwright_raises(monkeypatch) -> None:
    """When no connect_factory is injected and playwright isn't
    importable, _open raises MissingPlaywrightError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    pool = LightpandaPool()

    async def _go():
        async with pool.lease():
            pass

    with pytest.raises(MissingPlaywrightError):
        _run(_go())


def test_lightpanda_pool_honors_cdp_url_kwarg() -> None:
    pool = LightpandaPool(cdp_url="http://other-host:9999")
    assert pool._cdp_url == "http://other-host:9999"


def test_lightpanda_pool_default_cdp_url_from_env(monkeypatch) -> None:
    # The constant is read at module load time, so we can't override
    # via env post-import. We can at least assert the default value.
    from spider.browsers.lightpanda import LIGHTPANDA_CDP_URL

    pool = LightpandaPool()
    assert pool._cdp_url == LIGHTPANDA_CDP_URL


# ─── BrowserPool Protocol conformance ────────────────────────────────
def test_lightpanda_pool_satisfies_browser_pool_protocol() -> None:
    """Defensive: the Protocol is structural, but verifying isinstance
    catches API drift if the Protocol changes."""
    from spider.browsers.base import BrowserPool

    pool = LightpandaPool()
    assert isinstance(pool, BrowserPool)
