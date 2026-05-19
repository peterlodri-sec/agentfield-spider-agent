"""browser-use engine wrapper.

`browser-use <https://github.com/browser-use/browser-use>`_ is an
LLM-driven browser-automation library (langchain-backed). It pairs a
Chromium instance with an LLM that decides what to click / type /
scroll based on a natural-language task.

Architecture
------------

Two deployment modes:

  embedded   spider-agent's Python process drives browser-use directly.
             Heavy: pulls in playwright + langchain + browser-use deps.
             Best for "one task per call" use cases.

  sidecar    A separate browser-use/web-ui container runs Chromium +
             exposes CDP + VNC. spider-agent calls its HTTP API.
             Heavier overall but the LLM workload is isolated from
             the spider process.

The ``browser_act`` entry point below runs in *embedded* mode against a
session's existing ``BrowserContext`` — no second browser is spawned,
no CDP round-trip per call. The session must already exist (created
via ``POST /sessions``); we reuse its context's first page so cookies,
storage, and any prior nav state are preserved.

Optional dependency
-------------------

``browser-use`` plus ``langchain-openai`` are pulled in via the
``[browser-use]`` install extra. When the extra is not installed,
``browser_act`` returns a clear ``status="error"`` envelope instead of
raising — operators see "install the [browser-use] extra" rather than
a 500.

SSRF gating
-----------

Every navigation the Agent attempts passes through ``page.route`` →
``is_safe_external_url_with_dns`` + optional ``SPIDER_AGENT_ALLOWED_HOSTS``
allowlist. Unsafe URLs abort the request before any bytes leave the
container.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any

from spider.log import log

# Env vars consumed here. Documented in .env.example.
BROWSER_USE_CDP_URL = os.environ.get(
    "BROWSER_USE_CDP_URL",
    "http://browser-use:9222",
)
BROWSER_USE_VNC_URL = os.environ.get(
    "BROWSER_USE_VNC_URL",
    "vnc://browser-use:5900",
)
BROWSER_USE_HTTP_URL = os.environ.get(
    "BROWSER_USE_HTTP_URL",
    "http://browser-use:8080",
)
BROWSER_USE_DEFAULT_MODEL = os.environ.get(
    "BROWSER_USE_DEFAULT_MODEL",
    "openai/gpt-4o",
)
# OpenRouter base URL for the LangChain OpenAI-compatible client. Agents
# in the fleet drive LLMs through OpenRouter so a single API key fans out
# across providers. Override per deployment if needed.
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL",
    "https://openrouter.ai/api/v1",
)

# Default cap on Agent steps. Picked to bound LLM cost on a runaway
# task; the route accepts a per-call override in [1, 100].
DEFAULT_MAX_STEPS = 25


def get_chrome_cdp_url() -> str:
    """Return the CDP URL operators / clients can use to drive Chrome.

    Sourced from ``BROWSER_USE_CDP_URL``. Defaults to the compose
    service name; in k3s the operator overrides via the ConfigMap.

    The URL is NOT health-checked here — callers should hit
    ``/json/version`` on it to confirm the browser-use sidecar is up.
    The ``/chrome-conn`` route does this check.
    """
    return BROWSER_USE_CDP_URL


def get_vnc_url() -> str:
    """Return the VNC URL for visual debugging of the browser-use sidecar.

    Sourced from ``BROWSER_USE_VNC_URL``. Default is ``vnc://browser-use:5900``
    — only meaningful when the operator runs the browser-use/web-ui
    container with VNC enabled (it does by default).
    """
    return BROWSER_USE_VNC_URL


async def probe_chrome_alive() -> tuple[bool, str]:
    """Cheap CDP smoke check.

    Hits ``GET /json/version`` on the configured CDP URL. Returns
    ``(ok, detail)`` — detail is a one-line description suitable for
    the /health surface or operator logs.
    """
    import httpx

    url = BROWSER_USE_CDP_URL.rstrip("/") + "/json/version"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return False, f"cdp probe non-200: {r.status_code}"
            data = r.json()
            return True, f"cdp ok: browser={data.get('Browser', '?')}"
    except Exception as e:  # noqa: BLE001
        return False, f"cdp probe {type(e).__name__}: {e}"


# ─── browser_act ────────────────────────────────────────────────────
def _missing_dep_envelope(task: str, detail: str) -> dict[str, Any]:
    """Standard error envelope when the [browser-use] extra is absent."""
    return {
        "status": "error",
        "error": (
            "browser-use is not installed. Install via the "
            "`[browser-use]` extra: "
            "`pip install agentfield-spider-agent[browser-use]` or "
            "`uv sync --extra browser-use`."
        ),
        "detail": detail,
        "task": task,
    }


def _build_llm(model: str) -> Any:
    """Construct a LangChain chat model pointed at OpenRouter.

    Raises ``ImportError`` when ``langchain-openai`` isn't installed —
    callers catch this and surface a clean envelope.

    ``OPENROUTER_API_KEY`` (env) is required. Falls back to ``OPENAI_API_KEY``
    for convenience in deployments that already set the OpenAI key.
    """
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY (or OPENAI_API_KEY) is required for browser_act")
    return ChatOpenAI(
        model=model,
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=0.0,
    )


async def _install_ssrf_route(page: Any, allowed: frozenset[str] | None) -> None:
    """Install a ``page.route`` handler that runs the SSRF gate on every
    request the Agent's browser issues.

    Unsafe URLs are aborted (Playwright ``route.abort``); the Agent
    sees the navigation fail and either tries a different action or
    reports failure.
    """
    from spider.security import is_host_allowed, is_safe_external_url_with_dns

    async def _gate(
        route: Any, request: Any
    ) -> None:  # pragma: no cover - exercised via integration
        url = request.url
        if not await is_safe_external_url_with_dns(url):
            log("browser_act", state="ssrf-block", url=url[:120])
            await route.abort("blockedbyclient")
            return
        if not is_host_allowed(url, allowed):
            log("browser_act", state="allowlist-block", url=url[:120])
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await page.route("**/*", _gate)


def _coerce_result(history: Any) -> dict[str, Any]:
    """Best-effort flattening of browser-use ``AgentHistoryList``.

    The library's result shape evolves between versions. We pull the
    common-denominator fields and fall back to ``str(history)`` so the
    response always has SOMETHING for the caller.
    """
    out: dict[str, Any] = {}
    # urls_visited: most versions expose .urls() returning list[str]
    try:
        urls = history.urls()
        if isinstance(urls, list):
            out["urls_visited"] = [str(u) for u in urls]
    except Exception:  # noqa: BLE001
        pass
    # final answer: .final_result() or last action's extracted_content
    try:
        final = history.final_result()
        if final is not None:
            out["result"] = final
    except Exception:  # noqa: BLE001
        pass
    # step count
    with contextlib.suppress(Exception):
        out["steps_taken"] = int(history.number_of_steps())
    # success / failure indicator
    with contextlib.suppress(Exception):
        out["is_done"] = bool(history.is_done())
    if not out:
        out["result"] = str(history)
    return out


async def browser_act(
    task: str,
    *,
    session_id: str,
    sessions: Any,
    pool: Any,
    model: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    allowed_hosts: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run a browser-use Agent on ``task`` inside ``session_id``'s context.

    Returns a flat dict::

        {
          "status":         "ok" | "error",
          "task":           "...",
          "session_id":     "...",
          "model":          "...",
          "max_steps":      25,
          "steps_taken":    n,
          "is_done":        bool,
          "urls_visited":   [...],
          "result":         "<agent's final answer>",
          "ms":             <wall time>,
          "error":          "..."  (only on status=error)
        }

    Reuses the session's existing ``BrowserContext`` — no second
    browser, no CDP round-trip. The session must have been created
    on ``pool`` (otherwise ``SessionManager`` raises ``RuntimeError``).

    Optional dependency: requires ``browser-use`` + ``langchain-openai``.
    Missing them returns a status=error envelope, not a 500.
    """
    t_start = time.monotonic()
    model_used = model or BROWSER_USE_DEFAULT_MODEL

    log("browser_act", state="start", task=task[:80], session_id=session_id, model=model_used)

    # Lazy imports — gated on the [browser-use] extra. Catching
    # ImportError keeps the surface clean when the extra isn't installed.
    try:
        from browser_use import Agent, BrowserSession
    except ImportError as e:
        return _missing_dep_envelope(task, detail=f"ImportError: {e}")

    try:
        llm = _build_llm(model_used)
    except ImportError as e:
        return _missing_dep_envelope(task, detail=f"langchain-openai missing: {e}")
    except RuntimeError as e:
        return {
            "status": "error",
            "error": str(e),
            "task": task,
            "session_id": session_id,
            "model": model_used,
        }

    # Resolve the session's context via SessionManager. ``lease_page``
    # yields a fresh Page on the existing context, and closes it when
    # the with-block exits — the BrowserContext (and its cookies /
    # storage) survives for the next call.
    try:
        async with sessions.lease_page(session_id, pool=pool) as page:
            await _install_ssrf_route(page, allowed_hosts)
            # browser-use's Agent accepts an existing Page via
            # BrowserSession. The Agent will navigate using THIS page,
            # so SSRF route handlers we installed above apply to every
            # nav the LLM picks.
            try:
                browser_session = BrowserSession(page=page)
            except TypeError:
                # Older browser-use versions accept the page as a kwarg
                # to Agent directly. Fall back to that shape.
                browser_session = None

            agent_kwargs: dict[str, Any] = {"task": task, "llm": llm}
            if browser_session is not None:
                agent_kwargs["browser_session"] = browser_session
            else:
                agent_kwargs["page"] = page
            agent = Agent(**agent_kwargs)
            history = await agent.run(max_steps=max_steps)
    except KeyError:
        return {
            "status": "error",
            "error": f"session not found: {session_id}",
            "task": task,
            "session_id": session_id,
            "model": model_used,
        }
    except RuntimeError as e:
        # SessionManager raises RuntimeError when the session was created
        # on a different pool (backend mismatch).
        return {
            "status": "error",
            "error": str(e),
            "task": task,
            "session_id": session_id,
            "model": model_used,
        }
    except Exception as e:  # noqa: BLE001
        log("browser_act", state="error", err=f"{type(e).__name__}: {e}")
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "task": task,
            "session_id": session_id,
            "model": model_used,
            "ms": int((time.monotonic() - t_start) * 1000),
        }

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    result = _coerce_result(history)
    log(
        "browser_act",
        state="ok",
        session_id=session_id,
        steps=result.get("steps_taken"),
        ms=elapsed_ms,
    )
    return {
        "status": "ok",
        "task": task,
        "session_id": session_id,
        "model": model_used,
        "max_steps": max_steps,
        "ms": elapsed_ms,
        **result,
    }


__all__ = [
    "BROWSER_USE_CDP_URL",
    "BROWSER_USE_DEFAULT_MODEL",
    "BROWSER_USE_HTTP_URL",
    "BROWSER_USE_VNC_URL",
    "DEFAULT_MAX_STEPS",
    "OPENROUTER_BASE_URL",
    "browser_act",
    "get_chrome_cdp_url",
    "get_vnc_url",
    "probe_chrome_alive",
]
