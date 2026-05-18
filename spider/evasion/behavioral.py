"""Behavioral simulation — make a session look like a human is reading.

Called by ``EvasionBase.post_goto()`` after every ``page.goto()``.
Light touch: small dwell, partial scroll, brief mouse activity. The
goal is to produce ENGAGEMENT TIME that ML-based bot detectors
weight positively — not to fool sophisticated behavioral biometrics
(that's impossible in a CDP-driven browser).

Backend dispatch
----------------

Cloak (Chromium under CDP): full treatment — dwell + scroll + mouse
move with ``steps`` for smoothing.

Lightpanda: dwell + scroll only. ``page.mouse.move`` is unreliable in
the current nightly CDP.

Tuning constants are conservative to keep total ``post_goto`` time
under ~1.5s. Adjust ``DWELL_MAX_MS`` if profiles start failing the
"too fast" engagement-time tier of bot detection.
"""
from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Any

# Wall-time budget. Conservative defaults; total stays under ~1.5s.
DWELL_MIN_MS = 200
DWELL_MAX_MS = 800
SCROLL_STEPS = 3
SCROLL_DELAY_MS = 150


async def dwell_and_scroll(page: Any, *, backend: str = "cloak") -> None:
    """Brief dwell + scroll on the page.

    Total wall time: ~700-1500ms typically. Non-fatal: any error in
    ``page.mouse`` / ``page.evaluate`` is swallowed because the
    fetch's extraction step is what we actually care about.
    """
    try:
        # Initial dwell — reading time before scrolling.
        dwell_ms = random.uniform(DWELL_MIN_MS, DWELL_MAX_MS)
        await asyncio.sleep(dwell_ms / 1000.0)

        # Scroll in small increments. ``window.scrollBy`` via
        # ``page.evaluate`` because ``page.mouse.wheel`` isn't reliable
        # in Lightpanda CDP.
        for _ in range(SCROLL_STEPS):
            dy = random.randint(120, 360)
            try:
                await page.evaluate(
                    "(dy) => window.scrollBy({ top: dy, behavior: 'smooth' })",
                    dy,
                )
            except Exception:
                # Page may be navigating away; skip the scroll loop.
                break
            await asyncio.sleep(SCROLL_DELAY_MS / 1000.0)

        # Mouse move — cloak only. Lightpanda's mouse.move silently
        # no-ops in the current nightly.
        if backend == "cloak":
            with contextlib.suppress(Exception):
                await page.mouse.move(
                    random.randint(100, 600),
                    random.randint(100, 400),
                    steps=5,
                )
    except Exception:
        # post_goto is best-effort — don't fail the fetch if the page
        # is already broken before extraction.
        pass


__all__ = [
    "DWELL_MAX_MS",
    "DWELL_MIN_MS",
    "SCROLL_DELAY_MS",
    "SCROLL_STEPS",
    "dwell_and_scroll",
]
