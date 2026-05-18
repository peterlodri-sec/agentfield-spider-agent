# CLAUDE.md — agentfield-spider-agent

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

## Known half-built: crawl worker loop (TODO before relying on /crawl)

`POST /crawl` creates a job + enqueues its seed URL into
`crawler:crawl:{job_id}:queue`, and `spider/crawl.py:crawl_iter()`
implements one tick of the worker loop. **Nothing actually invokes
crawl_iter in a loop** as of the May 18 extension — jobs sit in
`status: "queued"` indefinitely. Verified locally: CloakBrowser
downloaded + initialized OK, but the dispatcher never picks the job up.

Fix needed: either
1. In `spider_server.py`, on aiohttp app startup, `asyncio.create_task(
   _crawl_dispatcher_loop(app))` that scans for any
   `crawler:crawl:*:queue` keys and calls `crawl_iter` per active job
   in a polite loop (same pattern as the existing async-fetch
   dispatcher for `crawler:queue`), OR
2. The compose architecture assumes `main.py` (the AF skill agent)
   hosts the dispatcher. If so: confirm in main.py + document the
   service split: HTTP-only spider_server can ACCEPT crawl jobs but
   not EXECUTE them.

Until this is fixed, the `scripts/check-crawl.sh` smoke will TIMEOUT
against any seed URL. Other surfaces (`/fetch/sync`, `/sitemap/parse`,
`/feeds`, `/jobs`, `/screenshot`, `/extract`) are unaffected.

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
