"""Lightpanda browser pool (BETA).

`Lightpanda <https://github.com/lightpanda-io/browser>`_ is a Zig
headless browser exposing Chrome DevTools Protocol on the network. We
don't manage the Lightpanda process; we connect via CDP. Operators
deploy Lightpanda as a sidecar (see ``Dockerfile.lightpanda`` +
``docker-compose.yml``) and set ``LIGHTPANDA_CDP_URL``.

Why a BETA tag
--------------

  - CDP coverage in Lightpanda is incomplete as of the nightly tag:
    no cookies API, partial JS framework support, no service workers,
    limited frame APIs. Fall back to CloakBrowser for sites that
    rely on these (most Cloudflare-protected ones, complex SPAs).
  - Page lifecycle semantics differ subtly from Chromium —
    ``wait_until="networkidle"`` may resolve earlier than expected.

When it shines
--------------

  - Tiny: ~10 MB binary vs Chromium's ~200 MB.
  - Cold-start ~100 ms vs Chromium ~1-3 s.
  - ~50 MB RAM/page vs Chromium's ~300 MB+.
  - Excellent for bulk crawls of static / lightly-dynamic sites.

Proxy
-----

The proxy is configured on the Lightpanda PROCESS (CLI flag or
environment), not on the pool here. ``SPIDER_PROXY_URL`` is surfaced
via ``proxy=true`` in the ``browser.opened`` log so operators can
sanity-check, but switching proxies at lease time isn't possible —
it'd mean restarting the Lightpanda container.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import AbstractAsyncContextManager
from typing import Any

from ..log import log
from ..proxy import env_proxy

BROWSER_IDLE_S = int(os.environ.get("BROWSER_IDLE_S", "60"))
LIGHTPANDA_CDP_URL = os.environ.get(
    "LIGHTPANDA_CDP_URL", "http://lightpanda:9222",
)
LIGHTPANDA_CONNECT_TIMEOUT_MS = int(
    os.environ.get("LIGHTPANDA_CONNECT_TIMEOUT_MS", "30000"),
)


class MissingPlaywrightError(RuntimeError):
    """Raised when playwright isn't importable.

    Install: ``pip install agentfield-spider-agent[lightpanda]``
    """


class LightpandaPool:
    """CDP-connected pool using Playwright as the client.

    Connects to an external Lightpanda CDP server. The Lightpanda
    process itself is NOT managed here. Operators run it via
    ``Dockerfile.lightpanda`` (built into the same docker-compose
    network) or directly. ``LIGHTPANDA_CDP_URL`` defaults to
    ``http://lightpanda:9222``.

    Lease semantics mirror ``CloakBrowserPool``: refcounted, idle
    reaper closes after ``BROWSER_IDLE_S`` of zero leases.
    """

    __slots__ = (
        "_browser",
        "_cdp_url",
        "_connect_factory",
        "_idle_at",
        "_leases",
        "_lock",
        "_playwright",
        "_reaper",
    )

    def __init__(
        self,
        *,
        cdp_url: str | None = None,
        connect_factory: Any = None,
        **_kw: Any,
    ) -> None:
        self._cdp_url: str = cdp_url or LIGHTPANDA_CDP_URL
        self._playwright: Any = None
        self._browser: Any = None
        self._leases: int = 0
        self._idle_at: float = 0.0
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        # Injectable for tests; production path imports lazily.
        self._connect_factory: Any = connect_factory

    async def _open(self) -> None:
        if self._connect_factory is not None:
            # Test path — fake replaces the playwright handshake.
            self._browser = await self._connect_factory(self._cdp_url)
            log(
                "browser",
                backend="lightpanda",
                state="opened",
                cdp_url=self._cdp_url,
                via="injected",
            )
            return

        try:
            from playwright.async_api import (
                async_playwright,
            )
        except ImportError as e:
            raise MissingPlaywrightError(
                "playwright is not installed. Install with: "
                "pip install agentfield-spider-agent[lightpanda]"
            ) from e

        t0 = time.perf_counter()
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._cdp_url,
                timeout=LIGHTPANDA_CONNECT_TIMEOUT_MS,
            )
        except Exception as e:  # pragma: no cover
            # Clean up the playwright runner if connect failed.
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            log(
                "browser",
                backend="lightpanda",
                state="connect-error",
                cdp_url=self._cdp_url,
                msg=f"{type(e).__name__}: {e}",
            )
            raise
        log(
            "browser",
            backend="lightpanda",
            state="opened",
            ms=int((time.perf_counter() - t0) * 1000),
            cdp_url=self._cdp_url,
            # Proxy is configured on the Lightpanda PROCESS, surfaced
            # here for visibility only.
            proxy=bool(env_proxy()),
        )

    async def _close_locked(self, *, reason: str) -> None:
        if self._browser is None:
            return
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        try:
            await browser.close()
        except Exception as e:  # pragma: no cover
            log(
                "browser",
                backend="lightpanda",
                state="close-error",
                msg=f"{type(e).__name__}: {e}",
            )
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
        log("browser", backend="lightpanda", state="closed", reason=reason)

    def lease(self) -> AbstractAsyncContextManager[Any]:
        """Yield a Page-like object from a fresh context on the
        Lightpanda CDP connection.
        """
        return self._lease_cm()

    @contextlib.asynccontextmanager
    async def _lease_cm(self):  # type: ignore[no-untyped-def]
        async with self._lock:
            if self._browser is None:
                await self._open()
            self._leases += 1
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap())
            # Reuse the default context Lightpanda exposes via CDP if
            # one exists; otherwise create a new one. Either way we
            # open a fresh page on it per lease.
            contexts = getattr(self._browser, "contexts", None) or []
            if contexts:
                ctx = contexts[0]
            else:
                ctx = await self._browser.new_context()
            page = await ctx.new_page()
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                await page.close()
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
        """Stop the reaper, close the connection. Idempotent."""
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
        async with self._lock:
            await self._close_locked(reason="shutdown")


__all__ = [
    "BROWSER_IDLE_S",
    "LIGHTPANDA_CDP_URL",
    "LIGHTPANDA_CONNECT_TIMEOUT_MS",
    "LightpandaPool",
    "MissingPlaywrightError",
]
