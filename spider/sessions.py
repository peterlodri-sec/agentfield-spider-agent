"""Browser session / context management.

A "session" here is an isolated `BrowserContext` on the active pool's
`Browser` — same concept as an incognito window in a regular browser.
Cookies, localStorage, IndexedDB, cache, and any persistent JS state
are scoped to the context. Two sessions are completely independent;
calling ``fetch(url, session_id="alice")`` from one browser tab and
``fetch(url, session_id="bob")`` from another effectively gives you
two separate users on the destination site.

Why callers want this
---------------------

  - Bot-detection bypass: rotate fingerprints across sessions, so a
    site that throttles one "user" doesn't block them all.
  - Logged-in scrapes: persist a cookie jar per account, reuse it
    across fetches within the same session.
  - Long-running narratives: an agent can run a multi-step browse
    (open site → log in → navigate → extract) and keep state between
    its calls.

API shape (v1)
--------------

  POST   /sessions                 → {session_id, ...info}
  GET    /sessions                 → {sessions: [info, ...]}
  GET    /sessions/{id}            → {...info}
  DELETE /sessions/{id}            → {ok: True}

  POST /fetch/sync {url, session_id}  reuses that session's context

Eviction policy
---------------

  - Hard cap: ``SPIDER_MAX_SESSIONS`` (default 100). On overflow, the
    least-recently-used session closes.
  - TTL: ``SPIDER_SESSION_TTL_S`` (default 7200 / 2 h). Sessions
    untouched for longer than this are closed on next access.

Persistence (v2 — not yet)
-------------------------

For now, sessions live in memory only. Container restart drops them.
A v2 enhancement will optionally persist `storage_state.json` per
session under a configurable volume so logged-in state survives.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .evasion import EvasionBase, EvasionConfig
from .log import log

# ─── env knobs ──────────────────────────────────────────────────────
MAX_SESSIONS: int = int(os.environ.get("SPIDER_MAX_SESSIONS", "100"))
SESSION_TTL_S: int = int(os.environ.get("SPIDER_SESSION_TTL_S", "7200"))

# Public/serializable session record. Returned by /sessions endpoints
# and the AgentField skill responses; pydantic so callers can validate
# without us hand-rolling dict shapes.

type Metadata = dict[str, Any]  # PEP 695


class SessionInfo(BaseModel):
    """Serializable session record.

    ``backend`` is the browser backend the session lives on (cloak or
    lightpanda). Sessions can't migrate between backends — if you
    switch the default backend in env, existing sessions become
    inaccessible (they get evicted on next sweep).
    """

    id: str
    name: str | None = None
    backend: str = "cloak"
    created_at: int = 0
    last_used: int = 0
    n_pages_opened: int = 0
    metadata: Metadata = Field(default_factory=dict)


@dataclass
class Session:
    """Internal session record.

    Holds the live ``BrowserContext`` plus the public ``SessionInfo``.
    Decoupled because the context can't go through Pydantic / JSON
    serialization.
    """

    info: SessionInfo
    context: Any  # Playwright BrowserContext
    pool_ref: Any = None  # Used to detect "did the pool change?"
    evasion: EvasionBase | None = None  # Bot-detection countermeasures


# ─── manager ────────────────────────────────────────────────────────
class SessionManager:
    """In-memory session registry, keyed by session id.

    Thread-safe under asyncio's single-thread guarantee — all
    mutating operations are awaited through ``_lock`` to keep the
    LRU + count invariants honest under concurrent creates.

    The manager doesn't own the browser pool; it takes a ``pool``
    argument per operation so a caller can run sessions on either
    backend without a tight coupling.
    """

    __slots__ = ("_sessions", "_lock", "_max", "_ttl_s")

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        ttl_s: int = SESSION_TTL_S,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._max = max_sessions
        self._ttl_s = ttl_s

    # ─── create ──────────────────────────────────────────────────────
    async def create(
        self,
        *,
        pool: Any,
        name: str | None = None,
        metadata: Metadata | None = None,
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None,
        backend_label: str = "cloak",
        persona: str | None = None,
        evasion_rate_per_min: int | None = None,
    ) -> SessionInfo:
        """Create a new session backed by a fresh BrowserContext.

        ``pool`` is the browser pool the session's context will live
        on. We grab the Browser via ``await pool.get_browser()`` (or
        fall back to opening it via ``pool.lease()`` for compatibility
        with the older CloakBrowserPool that doesn't expose
        ``get_browser`` directly).

        ``persona`` is an optional name from ``spider.evasion``'s
        ``PRESETS`` (call ``list_personas()`` for valid names). When
        set, the session gets a pinned identity (UA + locale + TZ +
        viewport + geo + Sec-CH-UA all coherent). Explicit
        ``user_agent`` / ``viewport`` kwargs override the persona's
        defaults.

        ``evasion_rate_per_min`` overrides the default per-session
        request budget (60). Set to a number <= 0 to disable. Setting
        either ``persona`` or ``evasion_rate_per_min`` attaches an
        ``EvasionBase`` to the session; otherwise the session has no
        evasion layer (legacy behavior preserved).
        """
        sid = uuid.uuid4().hex[:12]
        # Monotonic last_used — if two sessions are created in the
        # same millisecond, the second gets ts+1 so LRU eviction is
        # deterministic. ``int(time.time() * 1000)`` collides on fast
        # paths; the bump keeps the ordering well-defined.
        now = int(time.time() * 1000)
        if self._sessions:
            max_existing = max(s.info.last_used for s in self._sessions.values())
            now = max(now, max_existing + 1)

        # Build the evasion layer (if any) BEFORE opening the context
        # so we can pull persona-derived kwargs (UA, locale, TZ,
        # viewport, geo, headers) into the new_context call.
        evasion: EvasionBase | None = None
        if persona is not None or evasion_rate_per_min is not None:
            cfg = EvasionConfig(persona=persona, backend=backend_label)
            if evasion_rate_per_min is not None:
                cfg.rate_per_min = evasion_rate_per_min
            evasion = EvasionBase(cfg)

        # Open the browser if it isn't already, and reach into its
        # context constructor. Both LightpandaPool and CloakBrowserPool
        # have a `_browser` attribute populated after `_open` runs;
        # we ensure it's open by calling `lease()` briefly if needed.
        browser = await _get_browser(pool)

        # Context construction kwargs — same names Playwright uses, so
        # both backends accept the call signature. Persona-derived
        # kwargs go in first; explicit caller kwargs override.
        ctx_kwargs: dict[str, Any] = {}
        if evasion is not None:
            ctx_kwargs.update(evasion.context_kwargs())
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent
        if viewport:
            ctx_kwargs["viewport"] = viewport
        context = await browser.new_context(**ctx_kwargs)
        if evasion is not None:
            await evasion.apply_to_context(context)

        info = SessionInfo(
            id=sid,
            name=name,
            backend=backend_label,
            created_at=now,
            last_used=now,
            n_pages_opened=0,
            metadata=metadata or {},
        )
        session = Session(info=info, context=context, pool_ref=pool, evasion=evasion)

        async with self._lock:
            self._sessions[sid] = session
            await self._evict_locked()

        log("session", state="created", id=sid, name=name, backend=backend_label)
        return info

    async def get(self, session_id: str) -> SessionInfo | None:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        return s.info

    async def get_evasion(self, session_id: str) -> EvasionBase | None:
        """Return the session's ``EvasionBase``, or ``None`` if the
        session doesn't exist or wasn't created with evasion.

        Used by the fetch pipeline to call ``check_rate()`` before
        ``page.goto`` and ``post_goto(page)`` after. Returning ``None``
        means "skip evasion for this fetch" — both ``check_rate`` and
        ``post_goto`` are guarded by the None check at the callsite.
        """
        s = self._sessions.get(session_id)
        if s is None:
            return None
        return s.evasion

    async def list(self) -> list[SessionInfo]:
        """Snapshot of all session infos, newest-touched first."""
        now = int(time.time() * 1000)
        valid = [
            s.info for s in self._sessions.values() if (now - s.info.last_used) / 1000 < self._ttl_s
        ]
        valid.sort(key=lambda i: i.last_used, reverse=True)
        return valid

    async def close(self, session_id: str) -> bool:
        async with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        try:
            await s.context.close()
        except Exception as e:  # pragma: no cover
            log(
                "session",
                state="close-error",
                id=session_id,
                msg=f"{type(e).__name__}: {e}",
            )
        else:
            log("session", state="closed", id=session_id)
        return True

    async def close_all(self) -> None:
        """Shutdown helper — closes every session. Idempotent."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            with suppress(Exception):  # pragma: no cover
                await s.context.close()

    @asynccontextmanager
    async def lease_page(
        self,
        session_id: str,
        *,
        pool: Any,
    ) -> Any:
        """Yield a fresh ``Page`` on the session's context.

        Raises ``KeyError`` if the session doesn't exist. The page is
        closed when the context manager exits; the session's context
        persists for the next lease.
        """
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"session not found: {session_id}")

        # TTL check — expire-on-access keeps the manager from needing a
        # background sweep task.
        now_ms = int(time.time() * 1000)
        if (now_ms - s.info.last_used) / 1000 >= self._ttl_s:
            log("session", state="expired", id=session_id)
            await self.close(session_id)
            raise KeyError(f"session expired: {session_id}")

        # The session was created against a specific pool. If the
        # caller is leasing through a different pool, refuse —
        # contexts can't migrate between Browsers.
        if pool is not None and s.pool_ref is not None and pool is not s.pool_ref:
            raise RuntimeError(
                f"session {session_id} was created on a different pool; "
                "create a new session on this backend"
            )

        page = await s.context.new_page()
        s.info.n_pages_opened += 1
        # Monotonic bump — same rationale as create(). Without this,
        # touching session A and then creating session B in the same
        # millisecond would tie at LRU eviction.
        other_max = 0
        for other_id, other_s in self._sessions.items():
            if other_id != session_id and other_s.info.last_used > other_max:
                other_max = other_s.info.last_used
        s.info.last_used = max(now_ms, other_max + 1)
        try:
            yield page
        finally:
            with suppress(Exception):  # pragma: no cover
                await page.close()

    async def _evict_locked(self) -> None:
        """Enforce the hard cap. Caller MUST hold ``self._lock``.

        Closes least-recently-used sessions until ``len <= self._max``.
        Closing is async; we collect the contexts to close, release
        the lock-held data, then close outside the critical path? No
        — we hold the lock for now; eviction is rare and the close
        is fast (Playwright BrowserContext.close is local).
        """
        while len(self._sessions) > self._max:
            # Find the oldest by last_used.
            lru_id = min(
                self._sessions.keys(),
                key=lambda k: self._sessions[k].info.last_used,
            )
            s = self._sessions.pop(lru_id)
            with suppress(Exception):  # pragma: no cover
                await s.context.close()
            log("session", state="evicted-lru", id=lru_id)


async def _get_browser(pool: Any) -> Any:
    """Reach into the pool to retrieve its open ``Browser``.

    Both LightpandaPool and CloakBrowserPool keep the browser at
    ``pool._browser``. We open it via a no-op lease if needed —
    less invasive than adding a ``get_browser`` method to the
    Protocol just for SessionManager.
    """
    if getattr(pool, "_browser", None) is None:
        # Trigger pool open via a brief lease. The lease's page is
        # immediately discarded; the side effect is _browser being set.
        async with pool.lease() as _:  # noqa: F841
            pass
    return pool._browser  # noqa: SLF001


__all__ = [
    "MAX_SESSIONS",
    "Metadata",
    "SESSION_TTL_S",
    "Session",
    "SessionInfo",
    "SessionManager",
]
