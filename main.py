"""agentfield-spider-agent — AgentField skill surface.

Capabilities:
  - spider-agent.fetch(url, sync=True) -> dict
      sync=True   inline browser fetch via /fetch/sync; returns
                  {status, id, url, fetch_url, title, text, ms, ...}
      sync=False  enqueue; returns {queued: True, url}
  - spider-agent.feed_add(url, interval_s=3600, name=None) -> dict
      registers an RSS/Atom feed for periodic polling.

These wrap the HTTP surface in ``spider_server.py`` so the agent
process stays small (no browser pool, no Redis client in memory). The
HTTP surface — running as a sibling container in the same pod (k3s) or
as a sibling service in docker compose — owns all the heavy state.

Environment:
  AGENT_NODE_ID         default: spider-agent
  AGENTFIELD_SERVER     default: http://localhost:8080
  PORT                  default: 8005
  SPIDER_HTTP_URL       default: http://localhost:11700
  SPIDER_TIMEOUT_S      default: 60
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agentfield import Agent

from reasoners._spider import (
    _act,
    _crawl,
    _crawl_status,
    _extract,
    _feed_add,
    _feed_list,
    _feed_poll,
    _feed_remove,
    _fetch,
    _jobs_recent,
    _screenshot,
    _sitemap_parse,
)

logger = logging.getLogger("spider-agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Agent(
    node_id=os.getenv("AGENT_NODE_ID", "spider-agent"),
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
    version="0.1.0",
    description=(
        "Web fetch + RSS/Atom feed polling. CloakBrowser-backed; "
        "rag-stack crawler-compatible HTTP surface on :11700."
    ),
    dev_mode=True,
)


@app.skill()
async def fetch(
    url: str,
    sync: bool = True,
) -> dict[str, Any]:
    """Fetch a URL through CloakBrowser via the spider HTTP surface.

    Returns the structured outcome — on success (status="ok"): title,
    text, fetch_url, ms, id, ts. On non-success: status, error.

    sync=True (default)  inline fetch, returns the text in this call.
    sync=False           enqueue, returns {queued: True, url}.
    """
    return await _fetch(url=url, sync=sync)


@app.skill()
async def feed_add(
    url: str,
    interval_s: int = 3600,
    name: str | None = None,
) -> dict[str, Any]:
    """Register an RSS/Atom feed for periodic polling.

    The HTTP surface stores the feed in Redis (crawler:feeds); the
    poller wakes up when interval_s elapses and re-fetches it.
    """
    return await _feed_add(url=url, interval_s=interval_s, name=name)


@app.skill()
async def feed_list() -> dict[str, Any]:
    """List all registered feeds."""
    return await _feed_list()


@app.skill()
async def feed_remove(feed_id: str) -> dict[str, Any]:
    """Remove a feed by ID."""
    return await _feed_remove(feed_id)


@app.skill()
async def feed_poll(feed_id: str) -> dict[str, Any]:
    """Force-poll a feed immediately."""
    return await _feed_poll(feed_id)


@app.skill()
async def jobs_recent(limit: int = 50) -> dict[str, Any]:
    """List recent fetch jobs."""
    return await _jobs_recent(limit)


@app.skill()
async def crawl(
    url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    same_domain_only: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Recursively crawl from a seed URL.

    Returns a job_id; use crawl_status(job_id) to poll for completion.
    """
    return await _crawl(
        url,
        max_depth=max_depth,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        timeout_seconds=timeout_seconds,
    )


@app.skill()
async def crawl_status(job_id: str) -> dict[str, Any]:
    """Get the status of a crawl job."""
    return await _crawl_status(job_id)


@app.skill()
async def sitemap_parse(
    url: str,
    max_urls: int = 1000,
    queue: bool = False,
) -> dict[str, Any]:
    """Parse a sitemap.xml and optionally enqueue discovered URLs."""
    return await _sitemap_parse(url, max_urls=max_urls, queue=queue)


@app.skill()
async def screenshot(
    url: str,
    full_page: bool = True,
) -> dict[str, Any]:
    """Take a browser screenshot of a URL."""
    return await _screenshot(url, full_page=full_page)


@app.skill()
async def extract(
    url: str,
    selector: str,
    attr: str | None = None,
) -> dict[str, Any]:
    """Extract content from a URL using a CSS selector.

    If attr is provided, extracts the attribute value; otherwise extracts text.
    Returns a dict with {ok, matches: [...], ms}.
    """
    return await _extract(url, selector, attr=attr)


@app.skill()
async def act(
    task: str,
    session_id: str,
    model: str | None = None,
    max_steps: int | None = None,
    allowed_hosts: list[str] | None = None,
) -> dict[str, Any]:
    """Run a browser-use Agent against an existing session's context.

    ``task`` is a natural-language description of what to do. ``session_id``
    must reference a session created via ``session_create`` (the Agent
    reuses its ``BrowserContext`` — no second browser is launched).

    Returns a flat dict: ``status``, ``task``, ``session_id``, ``model``,
    ``ms``, optionally ``result`` / ``urls_visited`` / ``steps_taken``
    / ``is_done`` on success, or ``error`` on failure. When the
    ``[browser-use]`` install extra is not installed, returns a
    status=error envelope describing the missing dep (the HTTP surface
    responds 501 in that case).
    """
    return await _act(
        task,
        session_id,
        model=model,
        max_steps=max_steps,
        allowed_hosts=allowed_hosts,
    )


if __name__ == "__main__":
    # AgentField's app.run() handles SIGTERM/SIGINT internally and
    # unregisters the agent from the control plane on shutdown. The
    # try/finally is belt-and-braces so we always log the lifecycle
    # boundary — SREs use the "stopping" log to correlate with k8s
    # rollout events and `docker compose stop` exit codes.
    logger.info(
        "spider-agent starting node_id=%s port=%s",
        os.getenv("AGENT_NODE_ID", "spider-agent"),
        os.getenv("PORT", "8005"),
    )
    try:
        app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", "8005")),
            auto_port=False,
        )
    except KeyboardInterrupt:
        logger.info("spider-agent stopping reason=SIGINT")
    except Exception as e:
        logger.exception("spider-agent stopping reason=exception class=%s", type(e).__name__)
        raise
    finally:
        logger.info("spider-agent stopped")
