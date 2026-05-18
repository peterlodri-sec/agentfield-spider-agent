"""``BrowserPool`` Protocol.

The fetch pipeline (``spider/fetch.py``) is decoupled from which
browser engine renders the page. Both ``CloakPool`` (Chromium-stealth)
and ``LightpandaPool`` (Zig CDP) satisfy this Protocol; the dispatcher
hands either to ``fetch_one`` via the ``pool`` kwarg.

The contract is intentionally small — lease a page, shutdown the
pool — so adding a third backend (Puppeteer, vanilla Playwright,
Camoufox) only needs these two methods.
"""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrowserPool(Protocol):
    """Duck-typed pool interface.

    Implementations also carry a ``_browser`` attribute (truthy when the
    browser is open, ``None`` when idle / closed). The HTTP ``/health``
    endpoint checks it for the ``open|closed|absent`` field.

    ``lease()`` is an async context manager that yields a Page-like
    object supporting ``goto(url, wait_until, timeout)``,
    ``evaluate(js)``, and ``close()``. CloakBrowser and Playwright both
    satisfy this; the Lightpanda variant uses Playwright's CDP client
    against an external Lightpanda process.
    """

    _browser: Any  # truthy when open

    def lease(self) -> AbstractAsyncContextManager[Any]:
        """Yield a Page-like object; auto-closed on context exit."""
        ...

    async def shutdown(self) -> None:
        """Close the browser + clean up resources. Idempotent."""
        ...


__all__ = ["BrowserPool"]
