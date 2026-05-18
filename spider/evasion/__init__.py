"""EvasionBase package — bot-detection countermeasures for browser pools.

Public surface::

    EvasionConfig   per-session config (persona name + knobs)
    EvasionBase     applies the config to a BrowserContext + Page
    Persona         one coherent device identity (UA + locale + TZ + ...)
    list_personas   names of all registered presets
    get_persona     look up one preset by name

Wired into ``spider.sessions.SessionManager.create()``. The HTTP
surface (``POST /sessions``) accepts ``persona`` and
``evasion_rate_per_min`` fields.

Backend-aware: techniques that overlap with CloakBrowser's built-in
patches are skipped on the cloak backend to avoid double-patching
fingerprints. Lightpanda gets the full treatment.
"""

from __future__ import annotations

from .base import EvasionBase, EvasionConfig
from .identity import Persona, get_persona, list_personas
from .rate import RateLimiter

__all__ = [
    "EvasionBase",
    "EvasionConfig",
    "Persona",
    "RateLimiter",
    "get_persona",
    "list_personas",
]
