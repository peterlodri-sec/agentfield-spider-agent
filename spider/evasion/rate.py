"""Per-session token-bucket rate limiter.

Each session has its OWN budget (vs. the global ``HostBucket`` which
limits per-host across all sessions). A burst of 60 requests from one
session followed by another 60 in 30 seconds is a smoking-gun bot
signal; this limiter forces the session to pace at its configured
RPM.

Algorithm: leaky bucket with capacity = ``rate_per_min`` and a fill
rate of ``rate_per_min / 60`` tokens per second. ``acquire()`` blocks
until at least one token is available, then consumes it.

Disabled (no-op) when ``rate_per_min <= 0``.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Asyncio-safe per-session rate limiter.

    Use one instance per session. Call ``await acquire()`` before each
    page navigation.
    """

    __slots__ = ("_capacity", "_fill_rate", "_tokens", "_last", "_lock")

    def __init__(self, rate_per_min: int) -> None:
        if rate_per_min < 1:
            raise ValueError(f"rate_per_min must be >= 1; got {rate_per_min}")
        self._capacity: float = float(rate_per_min)
        # tokens per second
        self._fill_rate: float = rate_per_min / 60.0
        # Start full so the first request goes through immediately.
        self._tokens: float = float(rate_per_min)
        self._last: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._fill_rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Compute wait time outside the lock.
                wait_s = (1.0 - self._tokens) / self._fill_rate
            await asyncio.sleep(wait_s)

    def available(self) -> float:
        """Return the current token count without consuming."""
        now = time.monotonic()
        elapsed = now - self._last
        return min(self._capacity, self._tokens + elapsed * self._fill_rate)


__all__ = ["RateLimiter"]
