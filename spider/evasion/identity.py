"""Persona presets — pinned coherent identities for a session.

A "persona" is a frozen tuple of (user_agent, locale, timezone_id,
viewport, geolocation, Sec-CH-UA headers) that look like a real
device. The key insight: NEVER mix these. A US user-agent + German
locale + Tokyo IP geolocation is a smoking-gun bot signal in any
modern detection vendor's ensemble model. By pinning all six
attributes to one preset, we present a consistent identity for the
session's lifetime.

Updating presets
----------------

UAs + Chrome versions drift every 4-6 weeks. Bumping here is the right
place — not at every callsite. When adding a new persona:

  * Look up the real device's User-Agent from a current visitor log.
  * Match the Sec-CH-UA hints to the same Chrome major version.
  * Match the locale + timezone to the device's typical region.
  * Set the viewport to the device's typical resolution.

Don't randomize per-request. The persona is FIXED for the session;
randomizing it is itself a fingerprint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class Persona:
    """One coherent device identity.

    All fields must form a plausible combination. ``sec_ch_ua`` is the
    full Sec-CH-UA header value (the brand list). Leave blank for
    Safari personas — Safari doesn't send those hints.
    """

    name: str
    user_agent: str
    locale: str  # e.g. "en-US"
    accept_language: str  # e.g. "en-US,en;q=0.9"
    timezone_id: str  # e.g. "America/New_York"
    viewport: dict[str, int]  # {"width": 1920, "height": 1080}
    geolocation: dict[str, float] | None = None  # {"latitude": ..., "longitude": ...}
    sec_ch_ua: str = ""  # full Sec-CH-UA brand list, or "" to skip
    sec_ch_ua_platform: str = ""  # "Windows", "macOS", "Linux", "Android"
    sec_ch_ua_mobile: str = "?0"  # "?0" or "?1"

    def context_kwargs(
        self,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Convert to Playwright ``browser.new_context(**kwargs)`` shape.

        Returned dict can be merged with explicit user kwargs; explicit
        kwargs should take precedence (caller's responsibility).
        """
        headers: dict[str, str] = {"Accept-Language": self.accept_language}
        if self.sec_ch_ua:
            headers["Sec-CH-UA"] = self.sec_ch_ua
        if self.sec_ch_ua_platform:
            headers["Sec-CH-UA-Platform"] = f'"{self.sec_ch_ua_platform}"'
        # Mobile hint is always sent ("?0" desktop / "?1" mobile).
        headers["Sec-CH-UA-Mobile"] = self.sec_ch_ua_mobile
        if extra_headers:
            headers.update(extra_headers)
        out: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport": self.viewport,
            "extra_http_headers": headers,
        }
        if self.geolocation is not None:
            out["geolocation"] = self.geolocation
            out["permissions"] = ["geolocation"]
        return out


# Curated presets — one coherent device per name. Mixing inside a
# persona produces detectable inconsistency.
#
# Chrome version baseline (bump monthly if production starts seeing
# version-based blocks): 130.
PRESETS: dict[str, Persona] = {
    "desktop-us-en": Persona(
        name="desktop-us-en",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        timezone_id="America/New_York",
        viewport={"width": 1920, "height": 1080},
        geolocation={"latitude": 40.7128, "longitude": -74.0060},  # NYC
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not(A:Brand";v="99"',
        sec_ch_ua_platform="Windows",
        sec_ch_ua_mobile="?0",
    ),
    "desktop-de-de": Persona(
        name="desktop-de-de",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        locale="de-DE",
        accept_language="de-DE,de;q=0.9,en;q=0.8",
        timezone_id="Europe/Berlin",
        viewport={"width": 1920, "height": 1080},
        geolocation={"latitude": 52.5200, "longitude": 13.4050},  # Berlin
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not(A:Brand";v="99"',
        sec_ch_ua_platform="Windows",
        sec_ch_ua_mobile="?0",
    ),
    "desktop-macos-en": Persona(
        name="desktop-macos-en",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        timezone_id="America/Los_Angeles",
        viewport={"width": 1440, "height": 900},
        geolocation={"latitude": 37.7749, "longitude": -122.4194},  # SF
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not(A:Brand";v="99"',
        sec_ch_ua_platform="macOS",
        sec_ch_ua_mobile="?0",
    ),
    "mobile-android-en": Persona(
        name="mobile-android-en",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Mobile Safari/537.36"
        ),
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        timezone_id="America/Chicago",
        viewport={"width": 412, "height": 915},
        geolocation={"latitude": 41.8781, "longitude": -87.6298},  # Chicago
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not(A:Brand";v="99"',
        sec_ch_ua_platform="Android",
        sec_ch_ua_mobile="?1",
    ),
    "mobile-ios-en": Persona(
        name="mobile-ios-en",
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        timezone_id="America/Los_Angeles",
        viewport={"width": 393, "height": 852},
        geolocation={"latitude": 34.0522, "longitude": -118.2437},  # LA
        # iOS Safari doesn't send Sec-CH-UA hints.
        sec_ch_ua="",
        sec_ch_ua_platform="",
        sec_ch_ua_mobile="?1",
    ),
}


def list_personas() -> list[str]:
    """Return all registered persona names, alphabetically sorted."""
    return sorted(PRESETS.keys())


def get_persona(name: str) -> Persona:
    """Return the named persona; raises ``KeyError`` on unknown name."""
    p = PRESETS.get(name)
    if p is None:
        raise KeyError(
            f"unknown persona: {name!r}. Known: {', '.join(sorted(PRESETS.keys()))}"
        )
    return p


__all__ = ["PRESETS", "Persona", "get_persona", "list_personas"]
