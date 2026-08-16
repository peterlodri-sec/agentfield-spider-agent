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

Companion env var: ``SPIDER_PROXY_TRUST_INSECURE``. Opt-in TLS-verify
bypass for outbound HTTPS — needed when the proxy does HTTPS
interception (mitmproxy with a UA-spoof addon, for example) and the
proxy's CA isn't installed in the spider's trust store. Without this,
every HTTPS call surfaces ``CERTIFICATE_VERIFY_FAILED``. The "right"
fix is to install the proxy's CA cert; this flag is the documented
escape hatch for setups where that's impractical (ephemeral
containers, rotating CAs, dev iteration).

The setup guide for "phone as 4G/5G proxy" (Termux SOCKS5, Tailscale
egress node, or ssh -D) lives in ``docs/phone-as-proxy.md``.
"""

from __future__ import annotations

import os
from typing import Any

_ENV_NAME = "SPIDER_PROXY_URL"
_ENV_INSECURE = "SPIDER_PROXY_TRUST_INSECURE"


def env_proxy() -> str | None:
    """Return SPIDER_PROXY_URL trimmed of whitespace, or None."""
    raw = (os.environ.get(_ENV_NAME) or "").strip()
    return raw or None


def trust_insecure() -> bool:
    """Return True if ``SPIDER_PROXY_TRUST_INSECURE`` is set to a truthy value.

    Truthy: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else (unset, empty, ``0``, ``false``, ``no``, ``off``) is False.

    When True, callers should skip TLS certificate verification for
    outbound HTTPS. This is intended for MITM-style proxies (mitmproxy,
    Charles, Fiddler) where the proxy re-signs every TLS connection
    with its own CA. The proper fix is to install that CA in the trust
    store; this knob is the documented escape hatch.
    """
    raw = (os.environ.get(_ENV_INSECURE) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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

    When ``SPIDER_PROXY_TRUST_INSECURE`` is set, ``ssl=False`` is added
    so aiohttp skips TLS verification (needed for MITM proxies whose CA
    isn't in the trust store).
    """
    out: dict[str, Any] = {}
    p = resolve_proxy(per_call)
    if p:
        out["proxy"] = p
    if trust_insecure():
        out["ssl"] = False
    return out


def httpx_client_kwargs(per_call: str | None = None) -> dict[str, Any]:
    """kwargs to merge into ``httpx.AsyncClient(...)`` so every request
    made by that client honors the resolved proxy.

    httpx supports session-level proxy; instantiate the client once per
    fetch with this spread and you're done.

    When ``SPIDER_PROXY_TRUST_INSECURE`` is set, ``verify=False`` is
    added so httpx skips TLS verification (needed for MITM proxies
    whose CA isn't in the trust store).
    """
    out: dict[str, Any] = {}
    p = resolve_proxy(per_call)
    if p:
        out["proxy"] = p
    if trust_insecure():
        out["verify"] = False
    return out


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
    "trust_insecure",
]
