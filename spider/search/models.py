"""Shared types for ``spider/search``.

These are the public contracts engines + the dispatcher operate on.
Engines (``spider/search/brave.py`` etc.) build ``SearchHit`` and
return ``SearchResponse``. The dispatcher merges or chains responses.

Compatible-on-purpose with ``web-search-agent/reasoners/models.py`` so
the migration in PR-E (VIB-30) is a thin rename rather than a shape
change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Canonical engine names. Extend as engines ship; the dispatcher's
# registry maps these to engine instances.
Engine = Literal[
    "brave",
    "linkup",
    "perplexity",
    "tavily",
    "ddg",
    "hn",
    "github",
]

# Depth knob. ``"deep"`` is meaningful for engines that expose a
# deep-search mode (Linkup primarily); others silently treat it as
# ``"standard"``. Each engine module's docstring documents its
# semantics.
Depth = Literal["standard", "deep"]


class MissingCredentialsError(Exception):
    """An engine's required API key / env is not configured.

    Raised by engine ``search()`` implementations. The dispatcher
    short-circuits when the PRIMARY engine raises this; fallback
    engines that raise are logged + skipped, the chain continues.
    """


class SearchHit(BaseModel):
    """One search result.

    ``rank`` is 1-indexed within the producing engine's response.
    ``source_engine`` lets callers see which engine surfaced a given
    hit even after merge — useful for debugging fallback chains.
    """

    title: str
    url: str
    snippet: str
    rank: int = Field(ge=1)
    score: float | None = None
    source_engine: str


class SearchResponse(BaseModel):
    """A dispatcher response.

    ``engine`` is the engine that produced ``results`` (None when
    everything failed; check ``error`` then). ``fallback_chain`` is
    the ordered list of engines TRIED — useful for debugging "why did
    we end up on engine X instead of Y?".

    ``synthesis`` is populated by engines that return a narrative
    answer in addition to citations (currently only Perplexity);
    other engines leave it None.
    """

    query: str
    engine: str | None = None
    results: list[SearchHit] = Field(default_factory=list)
    fallback_chain: list[str] = Field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    synthesis: str | None = None


__all__ = [
    "Depth",
    "Engine",
    "MissingCredentialsError",
    "SearchHit",
    "SearchResponse",
]
