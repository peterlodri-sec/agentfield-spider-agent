"""EvasionBase + persona presets + rate limiter tests.

All offline — no real browser. Uses a FakeContext that captures the
init scripts and the new_context kwargs we pass, so we can assert
the persona's identity flows through to the right call sites.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from spider.evasion import (
    EvasionBase,
    EvasionConfig,
    Persona,
    RateLimiter,
    get_persona,
    list_personas,
)
from spider.evasion.behavioral import SCROLL_STEPS, dwell_and_scroll
from spider.evasion.network import WEBRTC_BLOCK_JS
from spider.sessions import SessionManager


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── fakes that mimic playwright shapes ─────────────────────────────
class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int, int]] = []

    async def move(self, x: int, y: int, *, steps: int = 1) -> None:
        self.moves.append((x, y, steps))


class _FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.evaluations: list[tuple[str, Any]] = []
        self.mouse = _FakeMouse()

    async def close(self) -> None:
        self.closed = True

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.evaluations.append((script, args[0] if args else None))
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False
        self.kwargs: dict[str, Any] = {}
        self.init_scripts: list[str] = []

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p

    async def close(self) -> None:
        self.closed = True

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        ctx = _FakeContext()
        ctx.kwargs = kwargs
        self.contexts.append(ctx)
        return ctx


class _FakePool:
    def __init__(self) -> None:
        self._browser = _FakeBrowser()


# ─── persona presets ────────────────────────────────────────────────
def test_list_personas_includes_expected_names() -> None:
    names = list_personas()
    assert "desktop-us-en" in names
    assert "desktop-de-de" in names
    assert "mobile-android-en" in names
    # Sorted.
    assert names == sorted(names)


def test_get_persona_returns_known() -> None:
    p = get_persona("desktop-us-en")
    assert isinstance(p, Persona)
    assert p.name == "desktop-us-en"
    assert "Windows NT" in p.user_agent
    assert p.locale == "en-US"
    assert p.timezone_id == "America/New_York"


def test_get_persona_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown persona"):
        get_persona("does-not-exist")


def test_persona_context_kwargs_shape() -> None:
    p = get_persona("desktop-de-de")
    kw = p.context_kwargs()
    assert kw["user_agent"] == p.user_agent
    assert kw["locale"] == "de-DE"
    assert kw["timezone_id"] == "Europe/Berlin"
    assert kw["viewport"] == {"width": 1920, "height": 1080}
    h = kw["extra_http_headers"]
    assert h["Accept-Language"].startswith("de-DE")
    assert h["Sec-CH-UA-Platform"] == '"Windows"'
    assert h["Sec-CH-UA-Mobile"] == "?0"
    # German persona uses Berlin geolocation.
    assert kw["geolocation"]["latitude"] == pytest.approx(52.52, rel=1e-3)
    assert kw["permissions"] == ["geolocation"]


def test_persona_extra_headers_merge() -> None:
    p = get_persona("desktop-us-en")
    kw = p.context_kwargs(extra_headers={"X-Custom": "1"})
    assert kw["extra_http_headers"]["X-Custom"] == "1"
    # Original headers still present.
    assert kw["extra_http_headers"]["Accept-Language"].startswith("en-US")


def test_ios_persona_skips_sec_ch_ua() -> None:
    p = get_persona("mobile-ios-en")
    kw = p.context_kwargs()
    h = kw["extra_http_headers"]
    # iOS Safari doesn't send these client hints.
    assert "Sec-CH-UA" not in h
    assert "Sec-CH-UA-Platform" not in h
    # Mobile flag still sent.
    assert h["Sec-CH-UA-Mobile"] == "?1"


# ─── rate limiter ───────────────────────────────────────────────────
def test_rate_limiter_rejects_zero_rpm() -> None:
    with pytest.raises(ValueError, match="rate_per_min"):
        RateLimiter(0)


def test_rate_limiter_starts_full_capacity_burst() -> None:
    """A fresh limiter has full capacity available for an initial burst."""
    rl = RateLimiter(rate_per_min=60)

    async def _go() -> float:
        t0 = time.monotonic()
        for _ in range(5):
            await rl.acquire()
        return time.monotonic() - t0

    elapsed = _run(_go())
    # 5 from a 60-bucket should be near-instant.
    assert elapsed < 0.1, f"5 from full bucket took {elapsed * 1000:.1f}ms"


def test_rate_limiter_blocks_when_empty() -> None:
    """After draining, the next acquire waits for refill."""
    rl = RateLimiter(rate_per_min=120)  # 2 tokens/sec

    async def _go() -> float:
        # Drain capacity.
        for _ in range(120):
            await rl.acquire()
        # Next acquire should wait ~0.5s (1 / 2 tokens/sec).
        t0 = time.monotonic()
        await rl.acquire()
        return time.monotonic() - t0

    elapsed = _run(_go())
    # Wide window for CI noise: 0.3s-1.0s.
    assert 0.3 < elapsed < 1.0, f"refill wait was {elapsed * 1000:.1f}ms"


def test_rate_limiter_available_decreases_after_acquire() -> None:
    rl = RateLimiter(rate_per_min=60)
    a0 = rl.available()

    async def _go() -> None:
        await rl.acquire()

    _run(_go())
    a1 = rl.available()
    assert a0 - a1 > 0.5


# ─── EvasionBase composition ────────────────────────────────────────
def test_evasion_no_persona_returns_empty_kwargs() -> None:
    ev = EvasionBase(EvasionConfig())
    assert ev.context_kwargs() == {}


def test_evasion_with_persona_returns_persona_kwargs() -> None:
    ev = EvasionBase(EvasionConfig(persona="desktop-us-en"))
    kw = ev.context_kwargs()
    assert "user_agent" in kw
    assert "Windows NT" in kw["user_agent"]


def test_evasion_unknown_persona_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown persona"):
        EvasionBase(EvasionConfig(persona="bogus"))


def test_evasion_apply_to_context_lightpanda_adds_webrtc_block() -> None:
    ev = EvasionBase(EvasionConfig(backend="lightpanda"))
    ctx = _FakeContext()
    _run(ev.apply_to_context(ctx))
    assert ctx.init_scripts == [WEBRTC_BLOCK_JS]


def test_evasion_apply_to_context_cloak_skips_webrtc_block() -> None:
    ev = EvasionBase(EvasionConfig(backend="cloak"))
    ctx = _FakeContext()
    _run(ev.apply_to_context(ctx))
    # Cloak handles its own; we don't double-patch.
    assert ctx.init_scripts == []


def test_evasion_block_webrtc_false_skips_regardless_of_backend() -> None:
    ev = EvasionBase(EvasionConfig(backend="lightpanda", block_webrtc=False))
    ctx = _FakeContext()
    _run(ev.apply_to_context(ctx))
    assert ctx.init_scripts == []


def test_evasion_check_rate_with_zero_rpm_noop() -> None:
    ev = EvasionBase(EvasionConfig(rate_per_min=0))

    async def _go() -> None:
        # No exception, no wait.
        await ev.check_rate()

    _run(_go())


def test_evasion_post_goto_disabled() -> None:
    """``behavioral_dwell=False`` short-circuits the post_goto pipeline."""
    ev = EvasionBase(EvasionConfig(behavioral_dwell=False))
    page = _FakePage()
    _run(ev.post_goto(page))
    assert page.evaluations == []
    assert page.mouse.moves == []


def test_evasion_post_goto_behavioral_simulates_scroll() -> None:
    ev = EvasionBase(EvasionConfig(backend="cloak"))
    page = _FakePage()
    _run(ev.post_goto(page))
    assert len(page.evaluations) == SCROLL_STEPS
    # Mouse move on cloak only.
    assert len(page.mouse.moves) == 1


def test_evasion_post_goto_skips_mouse_on_lightpanda() -> None:
    ev = EvasionBase(EvasionConfig(backend="lightpanda"))
    page = _FakePage()
    _run(ev.post_goto(page))
    assert page.mouse.moves == []
    # Scrolls still happen.
    assert page.evaluations


def test_dwell_and_scroll_handles_evaluate_failure() -> None:
    """Errors from ``page.evaluate`` inside the scroll loop don't propagate."""

    class _BrokenPage:
        async def evaluate(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("page closed")

    _run(dwell_and_scroll(_BrokenPage(), backend="lightpanda"))


# ─── SessionManager integration ─────────────────────────────────────
def test_session_manager_persona_populates_evasion_and_kwargs() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            name="alice",
            persona="desktop-us-en",
            backend_label="cloak",
        )

    info = _run(_go())
    ctx = pool._browser.contexts[0]
    # Persona's UA flowed into new_context kwargs.
    assert "Windows NT" in ctx.kwargs["user_agent"]
    assert ctx.kwargs["locale"] == "en-US"
    assert ctx.kwargs["timezone_id"] == "America/New_York"
    # Evasion attached.
    ev = _run(mgr.get_evasion(info.id))
    assert isinstance(ev, EvasionBase)
    assert ev.persona is not None
    assert ev.persona.name == "desktop-us-en"


def test_session_manager_no_persona_no_evasion() -> None:
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(pool=pool, name="bob")

    info = _run(_go())
    ev = _run(mgr.get_evasion(info.id))
    assert ev is None


def test_session_manager_explicit_kwargs_override_persona() -> None:
    pool = _FakePool()
    mgr = SessionManager()
    custom_ua = "Mozilla/5.0 (custom test)"

    async def _go():
        return await mgr.create(
            pool=pool,
            persona="desktop-de-de",
            user_agent=custom_ua,
            viewport={"width": 1024, "height": 768},
        )

    _run(_go())
    ctx = pool._browser.contexts[0]
    assert ctx.kwargs["user_agent"] == custom_ua
    assert ctx.kwargs["viewport"] == {"width": 1024, "height": 768}
    # Persona-derived locale/TZ still present (not overridden).
    assert ctx.kwargs["locale"] == "de-DE"


def test_session_manager_evasion_rate_only_no_persona() -> None:
    """``evasion_rate_per_min`` alone still attaches EvasionBase."""
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            evasion_rate_per_min=30,
        )

    info = _run(_go())
    ev = _run(mgr.get_evasion(info.id))
    assert ev is not None
    assert ev.config.rate_per_min == 30
    # No persona means no persona-derived ctx kwargs.
    ctx = pool._browser.contexts[0]
    assert "user_agent" not in ctx.kwargs


def test_session_manager_get_evasion_unknown_session() -> None:
    mgr = SessionManager()
    assert _run(mgr.get_evasion("nope")) is None


def test_session_manager_lightpanda_persona_installs_webrtc_init_script() -> None:
    """End-to-end: lightpanda + persona -> WebRTC block init script installed."""
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            persona="desktop-us-en",
            backend_label="lightpanda",
        )

    _run(_go())
    ctx = pool._browser.contexts[0]
    assert ctx.init_scripts == [WEBRTC_BLOCK_JS]


def test_session_manager_cloak_persona_skips_webrtc_init_script() -> None:
    """End-to-end: cloak -> no init script (cloak handles its own)."""
    pool = _FakePool()
    mgr = SessionManager()

    async def _go():
        return await mgr.create(
            pool=pool,
            persona="desktop-us-en",
            backend_label="cloak",
        )

    _run(_go())
    ctx = pool._browser.contexts[0]
    assert ctx.init_scripts == []
