"""SearchEngine Protocol.

Implementations live in sibling modules and are registered in the
dispatcher's ENGINES dict (PR-B-2). Class-based vs the older
module-based shape (web-search-agent) so each engine can cache its
auth headers, hold per-instance rate-limiter state, and be tested
without monkey-patching modules.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import Depth, SearchResponse


@runtime_checkable
class SearchEngine(Protocol):
    """Per-engine interface.

    Implementations:
      - Set ``name`` to the canonical key the dispatcher's ENGINES
        map uses (e.g. ``"brave"``).
      - Declare ``needs_key_env`` so the dispatcher's
        ``/search/engines`` discovery endpoint can report whether
        each engine is usable without trying it.
      - Implement ``search()`` to return ``SearchResponse`` with
        non-empty ``results`` on success.

    Engines should raise ``MissingCredentialsError`` on unset env;
    other failures should be caught and returned as
    ``SearchResponse(results=[], error=...)`` so the dispatcher can
    continue the fallback chain.

    ``client`` is an ``httpx.AsyncClient`` provided by the
    dispatcher — typed as ``Any`` here to keep the Protocol
    importable without an httpx dependency.
    """

    name: str
    needs_key_env: tuple[str, ...]

    async def search(
        self,
        *,
        client: Any,
        q: str,
        max_results: int = 10,
        depth: Depth = "standard",
        include_images: bool = False,
    ) -> SearchResponse: ...


__all__ = ["SearchEngine"]
