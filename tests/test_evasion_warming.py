"""Tests for EvasionBase pre-goto warming modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from spider.evasion import EvasionBase, EvasionConfig
from spider.fetch import FetchDeps, fetch_one
from spider.sessions import SessionManager


@dataclass
class _WarmPage:
    goto_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    click_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_goto: bool = False
    fail_click: bool = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self.fail_goto:
            raise RuntimeError("goto failed")
        self.goto_calls.append((url, kwargs))

    async def click(self, selector: str, **kwargs: Any) -> None:
        if self.fail_click:
            raise RuntimeError("click failed")
        self.click_calls.append((selector, kwargs))


@pytest.mark.asyncio
async def test_pre_goto_off_is_noop() -> None:
    evasion = EvasionBase(EvasionConfig(warming="off"))
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert page.goto_calls == []
    assert page.click_calls == []


@pytest.mark.asyncio
async def test_pre_goto_search_click_uses_google_and_clicks_on_cloak() -> None:
    evasion = EvasionBase(EvasionConfig(backend="cloak", warming="search-click"))
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0].startswith("https://www.google.")
    assert "q=" in page.goto_calls[0][0]
    assert len(page.click_calls) == 1


@pytest.mark.asyncio
async def test_pre_goto_search_click_skips_click_on_lightpanda() -> None:
    evasion = EvasionBase(EvasionConfig(backend="lightpanda", warming="search-click"))
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0].startswith("https://www.google.")
    assert page.click_calls == []


@pytest.mark.asyncio
async def test_pre_goto_locale_drives_google_tld() -> None:
    evasion = EvasionBase(
        EvasionConfig(persona="desktop-de-de", backend="cloak", warming="search-click")
    )
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0].startswith("https://www.google.de/search?")


@pytest.mark.asyncio
async def test_pre_goto_uses_custom_warming_query_when_provided() -> None:
    evasion = EvasionBase(
        EvasionConfig(
            backend="cloak",
            warming="search-click",
            warming_queries=["  custom topic query  "],
        )
    )
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert len(page.goto_calls) == 1
    assert "q=custom+topic+query" in page.goto_calls[0][0]


@pytest.mark.asyncio
async def test_pre_goto_hub_mode_visits_hub() -> None:
    evasion = EvasionBase(EvasionConfig(warming="hub"))
    page = _WarmPage()

    await evasion.pre_goto(page, "https://example.com/target")

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] in {"https://dev.to/", "https://github.com/trending"}
    assert page.click_calls == []


@pytest.mark.asyncio
async def test_pre_goto_is_best_effort_on_errors() -> None:
    evasion = EvasionBase(EvasionConfig(warming="search-click"))
    page = _WarmPage(fail_goto=True)

    await evasion.pre_goto(page, "https://example.com/target")

    # If the warmup goto fails, the method should swallow and proceed.
    assert page.click_calls == []


@dataclass
class _EvasionSpy:
    calls: list[Any] = field(default_factory=list)

    async def check_rate(self) -> None:
        self.calls.append("check_rate")

    async def pre_goto(self, page: Any, target_url: str) -> None:
        self.calls.append(("pre_goto", target_url))
        await page.goto("https://warmup.example/path", wait_until="load", timeout=5000)

    async def post_goto(self, page: Any) -> None:
        self.calls.append("post_goto")


class _SessionShim:
    def __init__(self, evasion: _EvasionSpy) -> None:
        self._evasion = evasion

    async def get_evasion(self, _session_id: str) -> _EvasionSpy:
        return self._evasion

    def lease_page(self, _session_id: str, *, pool: Any) -> Any:
        return pool.lease()


@dataclass
class _SessionCtx:
    async def new_page(self) -> _WarmPage:
        return _WarmPage()

    async def close(self) -> None:
        return None


@dataclass
class _SessionBrowser:
    contexts: list[_SessionCtx] = field(default_factory=list)

    async def new_context(self, **_kwargs: Any) -> _SessionCtx:
        ctx = _SessionCtx()
        self.contexts.append(ctx)
        return ctx


@dataclass
class _SessionPool:
    _browser: _SessionBrowser = field(default_factory=_SessionBrowser)

    async def ensure_browser(self) -> _SessionBrowser:
        return self._browser


@pytest.mark.asyncio
async def test_fetch_calls_pre_goto_before_target_navigation(make_deps, monkeypatch) -> None:
    deps, page = make_deps()
    evasion = _EvasionSpy()
    deps_with_session = FetchDeps(
        http=deps.http,
        pool=deps.pool,
        robots=deps.robots,
        bucket=deps.bucket,
        sessions=_SessionShim(evasion),
    )
    monkeypatch.setattr(
        "spider.fetch.is_safe_external_url_with_dns",
        AsyncMock(return_value=True),
    )

    out = await fetch_one(deps_with_session, "https://example.com/page", session_id="s1")

    assert out["status"] == "ok"
    assert evasion.calls == [
        "check_rate",
        ("pre_goto", "https://example.com/page"),
        "post_goto",
    ]
    assert page.goto_calls[0][0] == "https://warmup.example/path"
    assert page.goto_calls[1][0] == "https://example.com/page"


@pytest.mark.asyncio
async def test_session_create_with_warming_off_keeps_evasion_disabled() -> None:
    manager = SessionManager()
    info = await manager.create(pool=_SessionPool(), warming="off")

    evasion = await manager.get_evasion(info.id)
    assert evasion is None
