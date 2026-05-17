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

from reasoners._spider import _fetch, _feed_add

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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8005")),
        auto_port=False,
    )
