"""Outbound-proxy plumbing.

Single env var: ``SPIDER_PROXY_URL`` (e.g. ``socks5://10.0.0.42:1080`` or
``http://user:pass@phone-proxy:8888``). When set, ALL outbound HTTP
egresses through it — specialized HTTP fetchers, the redirect walker,
robots.txt fetches, and the CloakBrowser pool's launched browser.

Per-call override: handlers can pass ``proxy="..."`` on incoming
payloads to temporarily route ONE request through a different proxy
(useful for testing or per-host overrides). Browser-path requests
still use the env-level proxy because the pool's already running by
the time a call arrives.

The setup guide for "phone as 4G/5G proxy" (Termux SOCKS5, Tailscale
egress node, or ssh -D) lives in ``docs/phone-as-proxy.md``.
"""
from __future__ import annotations

import os
from typing import Any

_ENV_NAME = "SPIDER_PROXY_URL"


def env_proxy() -> str | None:
    """Return SPIDER_PROXY_URL trimmed of whitespace, or None."""
    raw = (os.environ.get(_ENV_NAME) or "").strip()
    return raw or None


def resolve_proxy(per_call: str | None = None) -> str | None:
    """Return the proxy URL to use for this call.

    Priority: ``per_call`` (explicit kwarg) > ``SPIDER_PROXY_URL`` env > None.
    Empty strings count as "no proxy" so callers can pass ``proxy=""``
    to forcibly bypass the env-configured proxy for one call.
    """
    if per_call is not None:
        return per_call.strip() or None
    return env_proxy()


def aiohttp_request_kwargs(per_call: str | None = None) -> dict[str, Any]:
    """kwargs to merge into ``aiohttp.ClientSession.get(...)`` so the
    request honors the resolved proxy.

    aiohttp doesn't support session-level proxy — it's per-request.
    Callers spread these kwargs into every ``get()`` / ``post()``.
    """
    p = resolve_proxy(per_call)
    return {"proxy": p} if p else {}


def httpx_client_kwargs(per_call: str | None = None) -> dict[str, Any]:
    """kwargs to merge into ``httpx.AsyncClient(...)`` so every request
    made by that client honors the resolved proxy.

    httpx supports session-level proxy; instantiate the client once per
    fetch with this spread and you're done.
    """
    p = resolve_proxy(per_call)
    return {"proxy": p} if p else {}


def cloakbrowser_launch_kwargs() -> dict[str, Any]:
    """kwargs to merge into ``cloakbrowser.launch_async(...)`` so the
    Chromium process egresses via the resolved proxy.

    Per-call override is NOT supported here because the browser pool
    is shared across requests; the proxy is fixed at pool launch. To
    swap proxies for a specific call, force ``use_browser=False`` and
    use the per-call kwarg on the HTTP fetcher path.
    """
    p = env_proxy()
    if not p:
        return {}
    # Playwright proxy shape — CloakBrowser is Playwright-compatible.
    return {"proxy": {"server": p}}


__all__ = [
    "aiohttp_request_kwargs",
    "cloakbrowser_launch_kwargs",
    "env_proxy",
    "httpx_client_kwargs",
    "resolve_proxy",
]
