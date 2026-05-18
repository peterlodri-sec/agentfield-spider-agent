"""CloakBrowser pool wrapper.

Replaces rag-stack's Scrapling-backed BrowserPool. Same lease /
refcount / idle-reaper semantics:

  - Zero cost at idle (no Chromium process).
  - First lease in a burst pays the launch (~1-3 s); subsequent leases
    in the same window share the browser instance and just open new
    pages.
  - After ``BROWSER_IDLE_S`` of refcount=0 the reaper closes the
    browser.

CloakBrowser is a Playwright drop-in (it patches Chromium at the C++
source level for stealth). Its async API is identical to Playwright's:
``await launch_async(...)`` returns a ``Browser``; ``await
browser.new_page()`` returns a ``Page``; ``Page.goto / content / close``
work unchanged.

If the cloakbrowser binary isn't installed (it auto-fetches a ~200 MB
Chromium on first use), the import fails with ``MissingDependencyError``
and the pod crash-loops — surfacing the config problem rather than
silently returning errors.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

from .log import log
from .proxy import cloakbrowser_launch_kwargs

# ─── env knobs ──────────────────────────────────────────────────────
HEADLESS = os.environ.get("FETCH_HEADLESS", "1") == "1"
NETWORK_IDLE = os.environ.get("FETCH_NETWORK_IDLE", "1") == "1"
DISABLE_RESOURCES = os.environ.get("FETCH_DISABLE_RES", "1") == "1"
FETCH_TIMEOUT_MS = int(os.environ.get("FETCH_TIMEOUT_MS", "30000"))
BROWSER_IDLE_S = int(os.environ.get("BROWSER_IDLE_S", "60"))
BROWSER_MAX_PAGES = int(os.environ.get("BROWSER_MAX_PAGES", "3"))


class MissingDependencyError(RuntimeError):
    """Raised when CloakBrowser isn't importable. Treat as fatal."""


class CloakBrowserPool:
    """Lazy, refcounted CloakBrowser with an idle reaper.

    Each ``lease()`` yields a fresh ``Page``; pages are closed on exit.
    The browser instance is shared across concurrent leases and is
    closed by the reaper after ``BROWSER_IDLE_S`` of zero leases.

    All construction args are forwarded to ``cloakbrowser.launch_async``.
    """

    __slots__ = (
        "_browser", "_idle_at", "_kwargs",
        "_launch_factory", "_leases", "_lock", "_reaper",
    )

    def __init__(
        self,
        *,
        launch_factory: Any = None,
        **kwargs: Any,
    ) -> None:
        self._kwargs = kwargs
        self._browser: Any = None
        self._leases: int = 0
        self._idle_at: float = 0.0
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        # Injectable for tests; production path imports cloakbrowser lazily
        # the first time we open the browser so we don't pay the binary
        # download cost on module import.
        self._launch_factory = launch_factory

    async def _open(self) -> None:
        if self._launch_factory is None:
            try:
                from cloakbrowser import launch_async  # type: ignore[import-not-found]
            except ImportError as e:
                raise MissingDependencyError(
                    "cloakbrowser is not importable. "
                    "Install via `pip install cloakbrowser` and let the "
                    "Chromium binary auto-fetch on first run."
                ) from e
            launcher = launch_async
        else:
            launcher = self._launch_factory

        proxy_kwargs = cloakbrowser_launch_kwargs()
        t0 = time.perf_counter()
        self._browser = await launcher(
            headless=HEADLESS,
            **proxy_kwargs,
            **self._kwargs,
        )
        log(
            "browser",
            state="opened",
            ms=int((time.perf_counter() - t0) * 1000),
            headless=HEADLESS,
            max_pages=BROWSER_MAX_PAGES,
            proxy=bool(proxy_kwargs),
        )

    async def _close_locked(self, *, reason: str) -> None:
        if self._browser is None:
            return
        browser, self._browser = self._browser, None
        try:
            await browser.close()
        except Exception as e:  # pragma: no cover
            log("browser", state="close-error", msg=f"{type(e).__name__}: {e}")
        else:
            log("browser", state="closed", reason=reason)

    @contextlib.asynccontextmanager
    async def lease(self):
        """Yield a fresh ``Page`` from the shared browser.

        Pages are closed on exit. The browser is opened on the first
        lease in an idle window; the reaper task runs while at least
        one lease has been taken in this window.
        """
        async with self._lock:
            if self._browser is None:
                await self._open()
            self._leases += 1
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap())
            page = await self._browser.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:  # pragma: no cover
                pass
            async with self._lock:
                self._leases -= 1
                if self._leases == 0:
                    self._idle_at = time.monotonic()

    async def _reap(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(BROWSER_IDLE_S, 10))
                async with self._lock:
                    if self._browser is None:
                        return
                    if self._leases > 0:
                        continue
                    if time.monotonic() - self._idle_at >= BROWSER_IDLE_S:
                        await self._close_locked(reason="idle")
                        return
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """Stop the reaper and close the browser. Idempotent."""
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
        async with self._lock:
            await self._close_locked(reason="shutdown")


__all__ = [
    "BROWSER_IDLE_S",
    "BROWSER_MAX_PAGES",
    "CloakBrowserPool",
    "DISABLE_RESOURCES",
    "FETCH_TIMEOUT_MS",
    "HEADLESS",
    "MissingDependencyError",
    "NETWORK_IDLE",
]
