"""Web search engine consolidation.

See ``docs/search-consolidation.md`` (Linear: [VIB-29]) for design.

Public surface (stubs only — PR-B-1)::

    SearchEngine             Protocol for per-engine implementations
    SearchHit                single result
    SearchResponse           full response with fallback chain
    Engine                   Literal of valid engine names
    Depth                    Literal["standard", "deep"]
    MissingCredentialsError  raised when an engine's env is unset

Engines and the dispatcher itself are intentionally absent here. They
land in:

  - PR-B-2 (VIB-28): port ``brave``, ``linkup``, ``perplexity`` from
    web-search-agent + wire ``dispatch_search(mode="primary")`` + HTTP
    route + AgentField skill.
  - PR-B-3: new engines (``tavily``, ``ddg``, ``hn``, ``github``).
  - PR-B-4: ``mode="merge"`` path with RRF.
"""

from __future__ import annotations

from .base import SearchEngine
from .models import (
    Depth,
    Engine,
    MissingCredentialsError,
    SearchHit,
    SearchResponse,
)

__all__ = [
    "Depth",
    "Engine",
    "MissingCredentialsError",
    "SearchEngine",
    "SearchHit",
    "SearchResponse",
]
