# agentfield-spider-agent

Web fetch + RSS/Atom feed polling for the agent fleet. CloakBrowser-backed
(source-level Chromium stealth) and Playwright-compatible. Two surfaces:

1. **AgentField skill** — `spider-agent.fetch(url, ...)` callable from any
   agent via `app.call(...)`.
2. **HTTP** — `:11700` matching the rag-stack crawler API so existing
   callers (rag-pocket pipelines etc.) can switch without code changes.

## Status

v1 complete. Both surfaces wired, k3s manifests in `deploy/k3s/`. End-to-end
smoke against a real CloakBrowser still requires `docker compose up` or
a k3s rollout — see "Local quick start" below.

## Capability

| Surface | Signature | Returns |
|---|---|---|
| AgentField | `spider-agent.fetch(url, sync=True)` | `{status, id, url, fetch_url, title, text, ms, ts}` on ok |
| AgentField | `spider-agent.feed_add(url, interval_s=3600, name=None)` | feed record |
| HTTP | `POST /fetch/sync {url}` | same dict as `fetch(sync=True)` |
| HTTP | `POST /fetch {url}` | `{queued: true, url}` (Redis-backed worker) |
| HTTP | `POST /feeds {url, interval_s?, name?}` | `{id, url, ...}` |
| HTTP | `GET /feeds` / `GET /feeds/{id}` / `DELETE /feeds/{id}` / `POST /feeds/{id}/poll` | feed CRUD + force-poll |
| HTTP | `GET /jobs?limit=50` | recent fetch outcomes |
| HTTP | `GET /health` | snapshot |

## Architecture

```
                ┌───────────────────────────────────────────────────┐
                │  agentfield-spider-agent  (one image, two procs)  │
   any agent ──▶│  main.py        ─── AgentField skill (port 8005) │──▶ Redis
   .call(…)     │                                                   │
   external ──▶ │  spider_server.py ── aiohttp HTTP (port 11700)    │──▶ CloakBrowser
                └───────────────────────────────────────────────────┘
```

Both processes share `spider/` (the browser pool, fetch pipeline,
politeness, security) so the two surfaces never drift.

## Local quick start

```bash
cp .env.example .env
# .env: AGENTFIELD_SERVER, REDIS_URL, browser knobs

docker compose up --build -d
docker compose logs -f spider-http | head -30

# Smoke (post-PR 1):
curl -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

## Engines / dependencies

| Concern | Library | Notes |
|---|---|---|
| Browser | [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) | Stealth Chromium with source-level patches. Playwright-compatible. ~200 MB binary auto-fetched on first run. |
| RSS/Atom | feedparser | Standard. |
| Redis | redis-py (asyncio) | Queue + seen-set + feeds + jobs. |
| HTTP | aiohttp | Server + outbound. |
| Agent framework | agentfield | The fleet's primitive. |

## License + redistribution note

**CloakBrowser:** the Python/TS wrapper is MIT, but the compiled Chromium
binary carries a non-redistribution license. We fetch it at container
runtime (not baked into the image) and cache it via a persistent volume
in k3s. Review the upstream license before public deployment.

## Provenance

This agent extracts the useful core of two existing tools:

- `rag-stack/scripts/crawler.py` — aiohttp + Scrapling crawler with feeds,
  politeness, robots, job log. We keep the HTTP shape and fetch pipeline,
  drop the topic / search-engine / rag-pocket plumbing.
- `bin/datapy/cmd/spider/` — Go binary with cookie-banner dismissal, DOM
  extraction, and Chrome guards. We port the cookie-banner + DOM-extraction
  layers to Python.

CloakBrowser replaces Scrapling as the browser backend.
