# AGENTS.md — agentfield-spider-agent

## Mission

Provide web fetch + RSS/Atom feed polling to the agent fleet (via
AgentField `@app.skill()`) and to any rag-stack-compatible HTTP caller
(via aiohttp on :11700). One Python core, two delivery surfaces.

## Role boundary with `camofox-agent`

The swarm has two anti-detect browser agents. They are NOT redundant —
they specialize:

| Use case | Agent | Why |
|---|---|---|
| Bulk crawl (depth-N) | **`spider-agent` (this one)** | Redis-backed queue, dedup, robots.txt + politeness, RSS/Atom feed poller, rag-stack HTTP API compat on :11700 |
| RSS / Atom feed monitoring | **`spider-agent`** | `feed_add` + scheduled poll baked in |
| Interactive multi-step browse | `camofox-agent` | `browse(actions=[...])` with click/type/snapshot ops on one tab |
| Visual debug (Dynamic Island, VNC) | `camofox-agent` | `fetch_page(watch=True)` returns vncUrl |
| OpenClaw plugin integration | `camofox-agent` | `openclaw.plugin.json` autoloads inside nemoclaw sandboxes |
| Anti-fingerprint at Firefox level | `camofox-agent` | Camoufox source-level fingerprint spoofing |
| Anti-fingerprint at Chromium level | **`spider-agent`** | CloakBrowser source-level Chromium stealth |

Default if you're unsure: `spider-agent` for "scrape N pages",
`camofox-agent` for "drive one browser like a human would".

## Architecture at a glance

- **Pattern:** deterministic dispatcher (no `.ai()`), one agent, externally
  callable skills, an aiohttp HTTP server speaking the rag-stack crawler
  API, shared `spider/` module so the two surfaces never drift.
- **Topology:** `main.py` runs the AgentField agent on :8005.
  `spider_server.py` runs the HTTP server on :11700. Two processes, one
  image, separate services in compose. In k3s: two containers in one pod
  (sidecar pattern) so the browser pool stays single-tenant per replica.
- **URL dispatcher:** `spider/dispatch.py:dispatch_fetch` classifies the
  URL (github / github-issue / hn / reddit / generic) and routes to the
  matching fetcher in `spider/fetchers/`. Generic falls through to the
  browser-rendered `fetch_one()` path. Each fetcher returns a flat-dict
  matching `fetch_one`'s shape — same keys (`id`, `url`, `status`,
  `title`, `text`, `ms`, `ts`) plus optional richer fields (`kind`,
  `name`, `description`, `metadata`). The routes layer + jobs ZSET
  contract is unchanged.
- **Browser:** CloakBrowser (replaces Scrapling). Source-level Chromium
  stealth patches; Playwright-compatible. Binary auto-fetched at first
  run (~200 MB), cached on a persistent volume in k3s.
- **Outbound proxy:** `SPIDER_PROXY_URL` env applies to ALL outbound
  HTTP — specialized fetchers, redirect walker, robots.txt, and the
  CloakBrowser pool (Chromium launched with the proxy). Per-call
  `proxy=` on `/fetch/sync` overrides for HTTP fetchers only. The
  browser pool is shared and its proxy is fixed at pool launch. Phone
  setup guide: `docs/phone-as-proxy.md`.
- **Storage:** Redis. `crawler:queue` (LIST), `crawler:seen` (SET),
  `crawler:feeds` (HASH), `crawler:jobs` (ZSET). External in production;
  bundled as a sidecar in `docker-compose.yml` for local dev.

## Primitive selection rules (binding)

- **No `.ai()` calls.** Deterministic only — no LLM in the fetch/extract
  path. (LLM-driven content scoring is a v2 feature, gated separately.)
- **`@app.skill()`, not `@app.reasoner()`.** Both surfaces share the same
  underlying Python; the skill wrapper is the canonical entry point.
- **No `app.harness(...)`.**
- **One browser per replica.** No fan-out / no shared global pool across
  containers; deduping happens via Redis `crawler:seen`.

## Data-flow rules

- `@app.skill('fetch')` returns `{title, text, links?, ms}` — flat dict,
  no nested response object.
- The HTTP `POST /fetch/sync` returns the same shape.
- `POST /fetch` (async) enqueues to `crawler:queue` and returns
  `{queued: true, url}`. The dispatcher worker drains the queue and
  writes outcomes to `crawler:jobs`.
- `MissingDependencyError` for cloakbrowser-import-failed is the explicit
  signal for "CloakBrowser binary not available". Treat as fatal — pod
  should crashloop, not silently return errors.

## Failure semantics

- Network / timeout / page-load error → record job as `"error"` with the
  exception class + message, return 502 (sync) or just record it (async).
- Robots-disallowed → record as `"blocked-by-robots"`, 403 (sync).
- SSRF-gate fail (loopback / private IP / blocklist) → record as
  `"blocked-by-ssrf-gate"`, 400.
- Empty extraction (< 100 chars) → record as `"empty"`, 200 with empty
  body. Caller's choice whether to retry.
- Crawl timeout exceeded → crawl status becomes `"completed"` when
  `max_pages` is hit or queue is empty.
- Sitemap parse error (non-200 HTTP, invalid XML, etc.) → returned as
  `errors` array in response; does not fail the request.
- Chromadb sink error → silently skipped (never fails primary fetch).
  Logged at `sink.exception` level.

## Runtime contract

- AgentField agent: `python main.py` (port **8005**) or `docker compose up`.
- HTTP server: `python spider_server.py` (port **11700**) or via compose.
- Both surfaces require `REDIS_URL` (default `redis://redis:6379/0` in
  compose).
- k3s: two containers in one pod, see `deploy/k3s/`.

## Crawl worker (`spider_server.py`)

`POST /crawl` enqueues into `crawler:crawl:{job_id}:queue`. On HTTP
server startup, `crawl_dispatcher` in `spider/crawl.py` scans
`crawler:crawl:*:state` and calls `crawl_iter` for jobs still
`queued` or `running` (tick interval `CRAWL_DISPATCH_TICK_S`, default
2s). The AgentField skill surface proxies to this HTTP server, so
crawl jobs execute in the `spider_server` process, not `main.py`.

## Hard rejections

- pocket / rag-pocket ingestion. The fetch returns text; callers persist
  if they want.
- Reddit-specific stealth (was in the Go spider — `reddit_stealth.go`).
  Out of scope. The reddit FETCHER (`spider/fetchers/reddit.py`) is
  API-based, not stealth — uses the `.json` endpoint with a browser
  UA + stripped tracking params.
- Bypassing `spider/security.py` for SSRF on the BROWSER path.
  Specialized HTTP fetchers (github / hn / reddit) hit known public
  APIs whose hosts are fixed (api.github.com,
  hacker-news.firebaseio.com, reddit.com), so the URL-construction is
  controlled by the fetcher and the SSRF gate doesn't apply to those
  outbound calls. The routes layer still runs layer-1 SSRF on the
  inbound URL before dispatch.
- Per-call BROWSER proxy. Pool is shared across requests; swapping
  the egress mid-flight breaks in-flight calls. To use a different
  proxy for one browser request, either restart the pool with a new
  `SPIDER_PROXY_URL`, or force `use_browser: false` so an HTTP
  fetcher honors the per-call `proxy` field.

## Browser backends (PR-BCD)

`spider/browsers/` is the registry. Two backends ship today:

| Backend | Module | Heft | When to use |
|---|---|---:|---|
| `cloak` | `spider/browsers/cloak.py` | ~200 MB Chromium | Default. Stealth source-level Chromium. Use for aggressively anti-bot sites (Cloudflare, Akamai). |
| `lightpanda` | `spider/browsers/lightpanda.py` | ~30 MB image | BETA. Zig browser with CDP. Tiny + fast (~100ms cold start, ~50MB/page). Coverage is partial — no cookies API, partial JS framework support. Falls back to cloak for sites it can't handle. |

Pick the default with `BROWSER_BACKEND=cloak|lightpanda` env. Per-call
override on `/fetch/sync` via the `browser` field. The dispatcher
lazy-instantiates the alternate pool on first use — unused backends
have zero cost.

Lightpanda runs as a separate process (compose service / k8s sidecar
/ standalone), not in the spider container. `Dockerfile.lightpanda`
builds the image (FROM the official `lightpanda/browser:nightly`).
`docker-compose.yml` defines it under the `lightpanda` profile —
start with `docker compose --profile lightpanda up`.

## Docling (PR-BCD)

`spider/docling_parser.py` handles PDF / Word / PowerPoint / Excel /
OpenDocument URLs. The dispatcher's `classify()` recognises common
document extensions and routes to docling instead of the browser
pool (Chromium can render PDFs but only as a single canvas with no
extractable text or table structure).

Heavy dependency: docling pulls in transformers + torch, ~1.5 GB of
ML models on first use. Install via the `[docling]` extra in
`pyproject.toml`; the standard image doesn't bundle it. Building
with `--build-arg INSTALL_DOCLING=1` (TODO: add to Dockerfile) opts
in. If docling isn't installed when the parser is invoked, the
function returns a `status="error"` envelope rather than raising —
keeps the dispatcher contract clean.

## Sessions / contexts (PR-OPT-CTX)

`spider/sessions.py` exposes `SessionManager` — an in-memory registry
of named browser sessions (= `BrowserContext`'s on the active pool's
`Browser`). Each session is isolated: cookies, localStorage, cache,
and persistent JS state are scoped to the context.

HTTP API:

  POST   /sessions                   create -> {id, name, backend, ...info}
  GET    /sessions                   list   newest-touched first
  GET    /sessions/{id}              get    404 if unknown / expired
  DELETE /sessions/{id}              close  free the context

  POST /fetch/sync {url, session_id}  reuses that session's context

Eviction:
  - Hard cap: `SPIDER_MAX_SESSIONS` (default 100). LRU on overflow.
  - TTL: `SPIDER_SESSION_TTL_S` (default 7200 / 2 h). Expired sessions
    raise `KeyError` on next access; the routes layer surfaces this
    as 404.

Sessions belong to one pool — leasing a session through a different
backend's pool raises `RuntimeError`. Switch backends ⇒ new session.

Persistence (storage_state to disk) is v2.

## Browser-use Agent (POST /act, spider-agent.act)

Natural-language browser automation against an existing session's
`BrowserContext` — no second browser is launched.

  POST /act
    {
      "task":          "find the latest release notes on docs.example.com",
      "session_id":    "<from POST /sessions>",
      "model":         "openai/gpt-4o",                 # optional
      "max_steps":     25,                              # optional, [1, 100]
      "allowed_hosts": ["example.com", ".docs.example.com"]  # optional
    }

The Agent loop runs inside `browser_act` in
`spider/browsers/browser_use_engine.py`. The LLM is a LangChain
`ChatOpenAI` pointed at OpenRouter (`OPENROUTER_API_KEY` env). Every
navigation the Agent attempts passes through a Playwright `page.route`
handler that runs `is_safe_external_url_with_dns` *and*, when
configured, the `SPIDER_AGENT_ALLOWED_HOSTS` allowlist. Unsafe URLs
abort with `blockedbyclient` before any bytes leave the container.

Optional dependency — install via the `[browser-use]` extra:

    pip install agentfield-spider-agent[browser-use]
    # or
    uv sync --extra browser-use

When the extra is not installed, `POST /act` returns HTTP `501` with
`{"status":"error","error":"browser-use is not installed..."}`. The
route plumbing itself works — operators can integrate before the
heavy LLM dep is on the image.

SSRF allowlist (`SPIDER_AGENT_ALLOWED_HOSTS`):

* Comma-separated host list. Empty / unset disables the allowlist
  (only the base SSRF gate runs).
* Prefix an entry with a dot (`.example.com`) to match the bare
  domain and any subdomain.
* Per-call `allowed_hosts` in the request body is unioned with the
  env-level set.
* The allowlist is an *additional* filter on top of
  `is_safe_external_url_with_dns` — private IPs / loopback / blocked
  hostnames are always rejected regardless.

## Build optimization (PR-OPT-CTX)

`Dockerfile.lightpanda` and `Dockerfile.cloak` are multi-stage with:
  - jemalloc LD_PRELOAD in the runtime layer (~15-30% memory wins
    on browser workloads with their small-alloc churn)
  - slim final layer (debian:trixie-slim + only runtime libs)
  - tini as PID-1 for clean SIGTERM forwarding
  - HEALTHCHECK wired (CDP `/json/version` for lightpanda)

Lightpanda supports two BUILD_MODE values:
  - `binary` (default) — fetches upstream release from GH
  - `source` — clones the lightpanda repo, builds with Zig +
    wild linker + clang/lld + jemalloc

Smart-build flow (`scripts/build_browser.sh`):
  1. Probe upstream version (GH release for lightpanda, PyPI for
     cloakbrowser).
  2. Probe `{REGISTRY}/{engine}-opt-ag:latest` for its
     `org.opencontainers.image.version` label.
  3. Match → exit 0 (no rebuild).
  4. Mismatch (or --force) → build, label, push, update `:latest`.

Usage:

```
REGISTRY=ghcr.io/peterlodri-sec scripts/build_browser.sh lightpanda
REGISTRY=ghcr.io/peterlodri-sec scripts/build_browser.sh lightpanda --mode=source
REGISTRY=ghcr.io/peterlodri-sec scripts/build_browser.sh cloak --force
```

The script outputs `image_ref=...` so CI / deploy scripts can pipe
the chosen tag into their `kubectl set image` or compose pull.

## Tooling

`pyproject.toml` is the canonical project config (modern uv-friendly
shape):

  - dependencies + optional extras (`docling`, `lightpanda`, `dev`)
  - ruff config (E/W/F/I/B/UP/SIM/RET/PIE/TID/C4/PYI, py312 target)
  - mypy strict (with per-module opt-outs for older modules)
  - pyright strict on the new code (`spider/browsers/**`,
    `spider/docling_parser.py`, `spider/dispatch.py`,
    `spider/proxy.py`, `spider/fetchers/**`)
  - pytest asyncio_mode=auto

`requirements.txt` is kept in sync for back-compat with the existing
Dockerfile install step. Regenerate from pyproject via
`uv export --no-hashes -o requirements.txt`.

New modules use Python 3.12 syntax: PEP 695 `type X = ...` aliases,
`X | None` unions, no `Dict`/`List` imports. Existing modules
gradually move into the strict-pyright list as they're typed up.

## Roadmap (remaining)

- **PR-B:** consolidate web-search engines (Linkup / Perplexity surfaces /
  Brave / DDG / Tavily / GitHub Code Search / HN Algolia) into spider.
  web-search-agent + pplx-agent become thin proxies until full deprecation.
- **PR-E:** delete web-search-agent + pplx-agent code; archive repos.
- (Maybe) Puppeteer pool as a third backend. Lightpanda + CloakBrowser
  cover the common cases; adding Puppeteer is now a `spider/browsers/`
  drop-in if a use case shows up.

## Delivery contract

Every change must preserve:

- `python -m py_compile main.py spider_server.py reasoners/*.py spider/*.py`
- `pytest -v --tb=short` — all unit tests pass without live network /
  browser. Browser-dependent paths use the mock pool in `tests/conftest.py`.
- `docker compose config > /dev/null` (compose syntax).
- The smoke ladder in `README.md`.

## Port assignment

| Port | Surface | Process |
|---|---|---|
| 8005 | AgentField skill | `main.py` |
| 11700 | HTTP API (rag-stack-compatible) | `spider_server.py` |

8005 chosen to avoid collision with: 8001 (my-agent), 8003 (pr-enricher),
8004 (web-search-agent), 8006 (data-engineer-agent).

## Security

- All outbound URLs pass through `spider/security.py`
  (`is_safe_external_url_with_dns` + `follow_safe_redirects`).
- Loopback, link-local, RFC1918 ranges, and a hostname blocklist are
  rejected pre-fetch.
- Redis: bind only via the in-cluster Service (k3s) or the compose
  network (local). No public exposure.
- No credentials are required for v1 — the browser fetches public pages.
  If we add authenticated scraping in v2, secrets go in a k8s Secret.
