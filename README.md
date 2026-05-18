# agentfield-spider-agent

Web fetch + RSS/Atom feed polling for the agent fleet. CloakBrowser-backed
(source-level Chromium stealth) and Playwright-compatible. Two surfaces:

1. **AgentField skill** — `spider-agent.fetch(url, ...)` callable from any
   agent via `app.call(...)`.
2. **HTTP** — `:11700` matching the rag-stack crawler API so existing
   callers (rag-pocket pipelines etc.) can switch without code changes.

## Status

v1.1 — URL-kind dispatcher + outbound proxy. `/fetch/sync` now routes
by URL shape (GitHub / HN / Reddit hit official APIs; everything else
goes through CloakBrowser). Proxy support via `SPIDER_PROXY_URL` for
phone-as-egress setups; see `docs/phone-as-proxy.md`.

## Capability

| Surface | Signature | Returns |
|---|---|---|
| AgentField | `spider-agent.fetch(url, sync=True)` | `{status, id, url, fetch_url, title, text, ms, ts}` on ok |
| AgentField | `spider-agent.feed_add(url, interval_s=3600, name=None)` | feed record |
| AgentField | `spider-agent.feed_list()` | `{feeds: [...]}` |
| AgentField | `spider-agent.feed_remove(feed_id)` | `{ok: true}` or error |
| AgentField | `spider-agent.feed_poll(feed_id)` | poll result dict |
| AgentField | `spider-agent.jobs_recent(limit=50)` | `{jobs: [...]}` |
| AgentField | `spider-agent.crawl(url, max_depth=2, max_pages=50, same_domain_only=True, timeout_seconds=600)` | `{job_id, status, ...}` |
| AgentField | `spider-agent.crawl_status(job_id)` | `{job_id, status, pages_fetched, pages_queued, ...}` |
| AgentField | `spider-agent.sitemap_parse(url, max_urls=1000, queue=False)` | `{url, urls: [...], count, queued}` |
| AgentField | `spider-agent.screenshot(url, full_page=True)` | `{ok, png_b64, ms}` |
| AgentField | `spider-agent.extract(url, selector, attr=None)` | `{ok, matches: [...], ms}` |
| HTTP | `POST /fetch/sync {url, use_browser?, proxy?}` | same dict as `fetch(sync=True)`, enriched per URL kind |
| HTTP | `POST /fetch {url}` | `{queued: true, url}` (Redis-backed worker) |
| HTTP | `POST /feeds {url, interval_s?, name?}` | `{id, url, ...}` |
| HTTP | `GET /feeds` / `GET /feeds/{id}` / `DELETE /feeds/{id}` / `POST /feeds/{id}/poll` | feed CRUD + force-poll |
| HTTP | `POST /crawl {url, max_depth?, max_pages?, same_domain_only?, timeout_seconds?}` | `{job_id, status, ...}` |
| HTTP | `GET /crawl/{job_id}` | crawl status |
| HTTP | `GET /crawl/{job_id}/results` | `{job_id, count, results: [...]}` |
| HTTP | `POST /sitemap/parse {url, queue?, max_urls?}` | `{url, urls: [...], count, queued}` |
| HTTP | `POST /screenshot {url, full_page?}` | `{ok, png_b64, ms}` |
| HTTP | `POST /extract {url, selector, attr?}` | `{ok, matches: [...], ms}` |
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

## URL-kind dispatcher

`/fetch/sync` (and the async dispatcher) routes by URL shape before
falling through to the browser:

| URL pattern | Fetcher | Surface |
|---|---|---|
| `github.com/{owner}/{repo}` | `spider/fetchers/github.py` | REST API + base64-decoded README |
| `github.com/{owner}/{repo}/issues/{N}` | `spider/fetchers/github.py` | REST API |
| `news.ycombinator.com/item?id={N}` | `spider/fetchers/hn.py` | Firebase API; recurses into GitHub fetcher if the linked URL is a repo |
| `reddit.com/...` | `spider/fetchers/reddit.py` | redirect-follow + `.json` with browser UA + stripped tracking params |
| anything else | `spider/fetch.py:fetch_one` | the original CloakBrowser-rendered path |

Each specialized fetcher returns the same flat-dict shape as
`fetch_one` (with extra keys: `kind`, `name`, `description`,
`metadata`), so callers that only read `status` / `title` / `text`
keep working.

Force the browser path explicitly with `use_browser: true` on the
request payload (or pass `use_browser=True` from Python).

## Outbound proxy

When `SPIDER_PROXY_URL` is set in the environment, ALL outbound HTTP
egresses through it — specialized fetchers, the redirect walker,
robots.txt, and the CloakBrowser pool's Chromium process.

Phone-as-egress setup (Termux SOCKS5, Tailscale exit node, or a
small Linux box on a phone hotspot) is documented in
[`docs/phone-as-proxy.md`](docs/phone-as-proxy.md).

Per-call override: `/fetch/sync` accepts a `proxy` field on the
payload. The override applies to the HTTP fetchers (github, hn,
reddit) only; the browser pool is shared across requests, so its
proxy is fixed at pool launch.

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
