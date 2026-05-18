"""Browser backend registry.

Two implementations:

  cloak       Default. CloakBrowser source-level Chromium stealth.
              Heavy (~200 MB Chromium binary), unrivalled for sites
              with aggressive anti-bot (Cloudflare, Akamai).

  lightpanda  BETA. Zig headless browser exposing CDP. Tiny (~10 MB),
              fast cold-start (~100 ms), low memory (~50 MB/page).
              Coverage is incomplete (no cookies API, partial JS
              framework support); falls back to cloak when it can't
              handle a page.

Pick the default at process startup via ``BROWSER_BACKEND`` env
(default: ``cloak``). Per-call override on ``/fetch/sync`` via the
``browser`` field — the dispatcher lazy-instantiates the alternate
pool on first use, so the unused backend has zero cost.

The two pools share a small Protocol (``BrowserPool`` in ``.base``) so
``fetch_one`` doesn't care which backend the lease comes from.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from .base import BrowserPool

# PEP 695 type alias — Python 3.12+
type Backend = Literal["cloak", "lightpanda"]

DEFAULT_BACKEND: Backend = "cloak"
_VALID_BACKENDS: frozenset[str] = frozenset({"cloak", "lightpanda"})


def normalize_backend(raw: str | None) -> Backend:
    """Map a caller-provided backend name to a canonical value.

    Unknown strings (or None) -> ``cloak`` default. The fail-soft
    behaviour is intentional: an unrecognized backend on a request
    payload shouldn't 5xx; it should just route to the safe default.
    """
    if raw is None:
        return DEFAULT_BACKEND
    cleaned = raw.strip().lower()
    if cleaned not in _VALID_BACKENDS:
        return DEFAULT_BACKEND
    return cleaned  # type: ignore[return-value]


def env_backend() -> Backend:
    """Read ``BROWSER_BACKEND`` env, normalized to a valid value."""
    return normalize_backend(os.environ.get("BROWSER_BACKEND"))


def make_pool(backend: Backend, **kwargs: Any) -> BrowserPool:
    """Construct a pool for ``backend``. Does NOT open the browser.

    Lazy: the underlying browser process / CDP connection is opened on
    the first ``.lease()``, not here. So instantiating both pools at
    startup is cheap (no Chromium boot, no Playwright spin-up).
    """
    if backend == "lightpanda":
        from .lightpanda import LightpandaPool
        return LightpandaPool(**kwargs)
    # Default + fallback: cloak
    from .cloak import CloakPool
    return CloakPool(**kwargs)


# ─── lazy registry for per-call alt backends ─────────────────────────
_lazy_pools: dict[Backend, BrowserPool] = {}
_lazy_lock: asyncio.Lock | None = None


def _get_lazy_lock() -> asyncio.Lock:
    """Return a module-level asyncio.Lock that's safe to construct
    after an event loop exists. We can't construct at import time
    because asyncio.Lock() needs a running loop in older Python.
    """
    global _lazy_lock
    if _lazy_lock is None:
        _lazy_lock = asyncio.Lock()
    return _lazy_lock


async def get_or_create_pool(
    backend: Backend, **kwargs: Any,
) -> BrowserPool:
    """Return the pool for ``backend``, constructing on first call.

    Callers that want to switch backends per request use this. The
    primary pool (the one wired into ``FetchDeps`` at startup) is also
    registered here so two-way switching shares the same instance.
    """
    async with _get_lazy_lock():
        if backend not in _lazy_pools:
            _lazy_pools[backend] = make_pool(backend, **kwargs)
        return _lazy_pools[backend]


def register_primary(backend: Backend, pool: BrowserPool) -> None:
    """Tell the registry which pool is the primary one.

    Called by ``spider_server.py`` after it constructs the deps pool,
    so that subsequent ``get_or_create_pool(backend)`` for the same
    backend returns THIS instance (not a duplicate).
    """
    _lazy_pools[backend] = pool


async def shutdown_all() -> None:
    """Close every pool the registry has spun up. Idempotent."""
    pools = list(_lazy_pools.values())
    _lazy_pools.clear()
    for p in pools:
        try:
            await p.shutdown()
        except Exception:  # pragma: no cover - shutdown is best-effort
            pass


__all__ = [
    "Backend",
    "BrowserPool",
    "DEFAULT_BACKEND",
    "env_backend",
    "get_or_create_pool",
    "make_pool",
    "normalize_backend",
    "register_primary",
    "shutdown_all",
]
