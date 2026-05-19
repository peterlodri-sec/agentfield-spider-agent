# agentfield-spider-agent

Web fetch + RSS/Atom feed polling for the agent fleet. CloakBrowser-backed
(source-level Chromium stealth) and Playwright-compatible. Two surfaces:

1. **AgentField skill** — `spider-agent.fetch(url, ...)` callable from any
   agent via `app.call(...)`.
2. **HTTP** — `:11700` matching the rag-stack crawler API so existing
   callers (rag-pocket pipelines etc.) can switch without code changes.

## Status

v1.2 — Adds Docling for structured doc parsing (PDF/Word/PPT) + a
beta `lightpanda` browser backend alongside CloakBrowser. Modern
tooling: `pyproject.toml` + uv + ruff + mypy/pyright strict on new
code. The dispatcher now routes 5 URL kinds (github / github-issue /
hn / reddit / docling) before falling through to the browser pool.

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
| AgentField | `spider-agent.act(task, session_id, model?, max_steps?, allowed_hosts?)` | `{status, task, session_id, model, ms, result?, urls_visited?, steps_taken?, is_done?}` |
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
| HTTP | `POST /act {task, session_id, model?, max_steps?, allowed_hosts?}` | `{status, task, session_id, model, ms, result?, urls_visited?, steps_taken?, is_done?}` |
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
| `*.pdf` / `*.docx` / `*.pptx` / etc. | `spider/docling_parser.py` | Docling-parsed markdown + tables (heavy dep, opt-in) |
| anything else | `spider/fetch.py:fetch_one` | CloakBrowser- or Lightpanda-rendered path |

Each specialized fetcher returns the same flat-dict shape as
`fetch_one` (with extra keys: `kind`, `name`, `description`,
`metadata`), so callers that only read `status` / `title` / `text`
keep working.

Force the browser path explicitly with `use_browser: true` on the
request payload (or pass `use_browser=True` from Python).

## Browser backends

| Backend | Module | Size | Use when |
|---|---|---:|---|
| `cloak` (default) | `spider/browsers/cloak.py` | ~200 MB Chromium | Stealth needed (Cloudflare, Akamai, aggressive WAFs) |
| `lightpanda` (beta) | `spider/browsers/lightpanda.py` | ~30 MB image | Bulk crawls of static / lightly-dynamic sites; latency or memory matter |

Pick the default via `BROWSER_BACKEND=cloak|lightpanda` env. Per-call
override on `/fetch/sync` payload:

```json
{
  "url": "https://example.com",
  "use_browser": true,
  "browser": "lightpanda"
}
```

The dispatcher lazy-instantiates the alternate pool on first use — the
unused backend has zero memory cost until it's needed.

Lightpanda runs as a separate process:

```bash
docker compose --profile lightpanda up -d
```

(See `Dockerfile.lightpanda` — it `FROM lightpanda/browser:nightly`
and serves CDP on `:9222`.)

## Docling (PDF / Word / PowerPoint structured parse)

URLs ending in a document extension (`.pdf`, `.docx`, `.pptx`,
`.xlsx`, `.odt`, ...) route to Docling instead of the browser pool —
Chromium can render PDFs but only as a single unstructured canvas;
Docling extracts markdown + tables + page counts.

Heavy dependency: docling pulls in transformers + torch and ~1.5 GB
of ML models on first use. Not bundled in the default image; install
via the `[docling]` extra:

```bash
pip install -e ".[docling]"
```

When docling isn't installed, the parser returns a clean
`status="error"` envelope rather than crashing.

Docling runtime tuning knobs:

- `SPIDER_DOCLING_TIMEOUT_S` (default `120`) — parse timeout
- `SPIDER_DOCLING_MAX_TABLES` (default `10`) — max tables extracted
- `SPIDER_DOCLING_TABLES_PREVIEW` (default `3`) — preview tables in metadata
- `SPIDER_DOCLING_MAX_TEXT_CHARS` (default `500000`) — response text cap

On `main` pushes, CI also builds and pushes a Docling-enabled image
(`INSTALL_DOCLING=1`) using the configured cloud Buildx builder and
tags it as `docling-latest` + `docling-$SHA`.

Manual push helper:

```bash
DOCKER_USER=your-user scripts/build-docling-image.sh
```

## Browser-use Agent (POST /act, spider-agent.act)

Natural-language browser automation against an existing session's
`BrowserContext` — no second browser is launched. The Agent loop
executes against the same Playwright page opened in `POST /sessions`,
reusing cookies, cache, and DOM state.

```json
POST /act
{
  "task": "find the latest release notes on docs.example.com",
  "session_id": "<from POST /sessions>",
  "model": "openai/gpt-4o",
  "max_steps": 25,
  "allowed_hosts": ["example.com", ".docs.example.com"]
}
```

Every URL navigation the Agent attempts passes through a Playwright
`page.route` handler that runs SSRF validation (`is_safe_external_url_with_dns`)
and, when configured, the `SPIDER_AGENT_ALLOWED_HOSTS` allowlist.
Unsafe URLs abort with `blockedbyclient` before any bytes leave the container.

Optional dependency — install via the `[browser-use]` extra:

```bash
pip install agentfield-spider-agent[browser-use]
# or
uv sync --extra browser-use
```

When the extra is not installed, `POST /act` returns HTTP 501 with
`{"status":"error","error":"browser-use is not installed..."}`. The
route plumbing works — operators can integrate the session pipeline
before the heavy LLM dependency is on the image.

**SSRF allowlist** (`SPIDER_AGENT_ALLOWED_HOSTS`):

- Comma-separated host list. Empty / unset disables the allowlist
  (only the base SSRF gate runs).
- Prefix an entry with a dot (`.example.com`) to match the bare domain
  and any subdomain.
- Per-call `allowed_hosts` in the request body is unioned with the
  env-level set.
- The allowlist is an *additional* filter on top of
  `is_safe_external_url_with_dns` — private IPs / loopback / blocked
  hostnames are always rejected regardless.

**Smoke test:**

```bash
bash scripts/check-act.sh
```

Returns 200 when browser-use is installed and working; 501 if the extra
is not installed (which is treated as a pass — route plumbing is healthy).

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

## Agent/dev bootstrap scripts

Environment bootstrap is centralized in:

- `.environment/setup.sh` (shared)
- `.environment/cursor/setup.sh`
- `.environment/codex/setup.sh`
- `.environment/claude/setup.sh`

The scripts install `requirements.txt`, validate with `pip check`, and
optionally install Docling when `SPIDER_INSTALL_DOCLING=1` (legacy
`SPIDER_DOCLING_ENABLED=1` is still accepted).

By default the same setup path also installs Docker CLI + buildx
(`SPIDER_INSTALL_DOCKER=1`) and performs `docker login` when both
`DOCKER_USER` and `DOCKER_PAT` are present (registry defaults to
`DOCKER_REGISTRY=docker.io`).

If your environment lacks root/sudo, setup now warns and continues by
default. Set `SPIDER_INSTALL_DOCKER_STRICT=1` to force a hard failure
instead.

Bootstrap validation helper:

```bash
./scripts/check-env-bootstrap.sh
```

For local containerized development, `.devcontainer/devcontainer.json`
uses `.devcontainer/postCreate.sh`, which delegates to the same shared
bootstrap script.

## Engines / dependencies

| Concern | Library | Notes |
|---|---|---|
| Browser (default) | [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) | Stealth Chromium with source-level patches. Playwright-compatible. ~200 MB binary auto-fetched on first run. |
| Browser (beta) | [Lightpanda](https://github.com/lightpanda-io/browser) | Zig browser exposing CDP. Tiny (~10 MB binary). Connect via Playwright `connect_over_cdp`. |
| Doc parse (opt-in) | [Docling](https://github.com/docling-project/docling) | PDF/Word/PPT/Excel → structured markdown + tables. ~1.5 GB models on first use. |
| Browser Agent (opt-in) | [browser-use](https://github.com/browser-use/browser-use) | Natural-language task automation via LLM Agent loop. Heavy dep: langchain + browser-use. Install `[browser-use]` extra. |
| RSS/Atom | feedparser | Standard. |
| Redis | redis-py (asyncio) | Queue + seen-set + feeds + jobs. |
| HTTP | aiohttp + httpx | Server (aiohttp) + outbound HTTP fetchers (httpx). |
| Agent framework | agentfield | The fleet's primitive. |

## Tooling

| Tool | Config | Purpose |
|---|---|---|
| `uv` | `pyproject.toml` `[tool.uv]` | Faster pip replacement; `uv sync` + `uv lock` |
| `ruff` | `[tool.ruff]` | Lint + format; py312 target |
| `mypy` | `[tool.mypy]` strict | Type check (per-module opt-outs for older modules) |
| `pyright` | `[tool.pyright]` strict on new modules | Editor + CI types |
| `pytest` | `[tool.pytest.ini_options]` asyncio_mode=auto | Tests |

Strict-typed modules (PEP 695 syntax, no `Dict`/`List` imports):
`spider/browsers/**`, `spider/docling_parser.py`, `spider/dispatch.py`,
`spider/proxy.py`, `spider/fetchers/**`. Older modules migrate in
follow-up PRs.

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
