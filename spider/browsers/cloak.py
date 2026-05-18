"""CloakBrowser pool — adapter into the unified ``BrowserPool`` Protocol.

The implementation lives in ``spider/browser.py`` (kept there to avoid
breaking existing imports). This module re-exports it under the
``CloakPool`` alias so callers can refer to ``spider.browsers.cloak``
uniformly with ``spider.browsers.lightpanda``.
"""
from __future__ import annotations

from typing import Any

from ..browser import CloakBrowserPool


class CloakPool(CloakBrowserPool):
    """CloakBrowser pool. Identical to ``spider.browser.CloakBrowserPool``;
    distinct name so the browsers/ namespace has parallel classes.
    """

    # No body needed — just an alias subclass for namespace consistency.
    pass


def __dir__() -> list[str]:
    return ["CloakPool"]


__all__: list[str] = ["CloakPool"]
