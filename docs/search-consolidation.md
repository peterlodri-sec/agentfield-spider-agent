# Search engine consolidation (PR-B) — design

**Status:** design — implementation phased into PR-B-1..4
**Linear:** [VIB-29](https://linear.app/vibepeter/issue/VIB-29) (this design) ▪ [VIB-28](https://linear.app/vibepeter/issue/VIB-28) (dispatcher impl) ▪ [VIB-30](https://linear.app/vibepeter/issue/VIB-30) (deprecate predecessors)
**Mirrored to:** [Linear document](https://linear.app/vibepeter/document/search-consolidation-pr-b-design-93b0630827c4) (kept in sync; this file is source of truth)

## Why

Today the fleet has three search-related services:

| Service | What it does | Cost |
|---|---|---|
| `web-search-agent` | Skill `web_search(q, eng, ...)` with linkup / perplexity / brave engines + fallback chain | one container, one AgentField node, one set of API keys |
| `pplx-agent` | Perplexity-specific skill (`ask`, `reason`, `research`, `search`) | another container + node |
| `spider-agent` | Fetch, crawl, sessions, evasion, profiles | the heavy one |

Three nodes. Three sets of secrets. Three healthchecks. And the common usage pattern — `search → fetch` — always crosses a process boundary.

After consolidation:

- `spider-agent` is the single service for **search + crawl + fetch + parse**.
- Shared `httpx.AsyncClient` (already used by the URL-kind fetchers and Docling).
- Shared `RateLimiter` infrastructure (already in `spider/evasion/rate.py`).
- One AgentField node to register and monitor.
- `web-search-agent` + `pplx-agent` become thin proxies for one release, then archive ([VIB-30](https://linear.app/vibepeter/issue/VIB-30)).

## Existing surface to preserve

`web-search-agent`'s public skill:

```python
web-search-agent.web_search(
    q: str,
    eng: Literal["linkup", "perplexity", "brave"] = "linkup",
    max_results: int = 10,
    depth: Literal["standard", "deep"] = "standard",
    include_images: bool = False,
    fallback_engines: list[str] | None = None,
) -> SearchResponse
```

Return shape:

```python
class SearchResponse(BaseModel):
    query: str
    engine: str | None              # which engine produced results
    results: list[SearchHit]
    fallback_chain: list[str]       # engines tried, in order
    error: str | None
    duration_seconds: float

class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    rank: int                       # 1-indexed
    score: float | None             # engine-specific relevance
    source_engine: str
```

The dispatcher (`reasoners/_search.py`) iterates the fallback chain, calls `engine_module.search(...)` on each, returns first non-empty. Errors are caught + recorded in `last_error`; the function never raises.

## Proposed module layout

```
spider/search/
├── __init__.py           public exports
├── models.py             SearchHit, SearchResponse, Engine, Depth, errors
├── base.py               SearchEngine Protocol
├── dispatch.py           dispatch_search() — primary + merge modes
├── brave.py              port from web-search-agent
├── linkup.py             port from web-search-agent
├── perplexity.py         port from web-search-agent
├── tavily.py             new — REST API, LLM-friendly
├── ddg.py                new — DuckDuckGo instant answers + HTML fallback
├── hn.py                 new — Algolia HN search API
└── github.py             new — GitHub code search (reuses GITHUB_TOKEN)
```

Mirrors `spider/fetchers/` (specialized HTTP clients, all sharing `httpx_client_kwargs` from `spider/proxy.py`).

## `SearchEngine` Protocol

Class-based protocol vs the existing module-based one. Class-based lets each engine cache auth headers, hold per-instance rate-limiter state, and be tested in isolation.

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class SearchEngine(Protocol):
    name: str                          # canonical name, e.g. "brave"
    needs_key_env: tuple[str, ...]     # required env vars, for fail-fast

    async def search(
        self,
        *,
        client: httpx.AsyncClient,
        q: str,
        max_results: int = 10,
        depth: Depth = "standard",
        include_images: bool = False,
    ) -> SearchResponse: ...
```

Engines raise `MissingCredentialsError` when their required env is missing. The dispatcher short-circuits the chain when the PRIMARY engine has missing credentials (current behavior preserved); for FALLBACK engines, missing credentials are logged + the chain continues.

## Dispatcher

```python
async def dispatch_search(
    *,
    q: str,
    engines: list[str] | None = None,  # default: SPIDER_SEARCH_FALLBACK_CHAIN env
    mode: Literal["primary", "merge"] = "primary",
    max_results: int = 10,
    depth: Depth = "standard",
    include_images: bool = False,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 10.0,
) -> SearchResponse: ...
```

Two modes:

| Mode | Behavior |
|---|---|
| `primary` | Try engines in order, return first non-empty. Matches current web-search-agent semantics. |
| `merge` | Query ALL engines in parallel (per-engine timeout), reciprocal-rank-fusion merge results. Higher signal but proportional cost. |

`SPIDER_SEARCH_FALLBACK_CHAIN` env: comma-separated engine names for the default `engines=` value (e.g. `brave,linkup,ddg`). Falls back to `["brave"]` if unset.

## HTTP surface

```
POST   /search/sync     {q, engines?, mode?, max_results?, depth?, include_images?}
                        → SearchResponse JSON

GET    /search/engines  → {engines: [{name, has_key, needs_key_env, rate_per_min}]}
```

`/search/engines` is a discovery endpoint — useful for clients to know which engines are usable in the current deployment without trying them.

## AgentField skill surface

```python
@app.skill("search")
async def search_skill(
    q: str,
    engines: list[str] | None = None,
    mode: str = "primary",
    max_results: int = 10,
    depth: str = "standard",
) -> dict:
    return (await dispatch_search(...)).model_dump()
```

Mirrors `web-search-agent.web_search` so existing callers can swap the skill name and keep working.

## Env vars per engine

| Engine | Required env | Notes |
|---|---|---|
| brave | `BRAVE_API_KEY` | Free tier ~2K queries/mo |
| linkup | `LINKUP_API_KEY` | Best for "deep" depth queries |
| perplexity | `PERPLEXITY_API_KEY` | Returns synthesized answers + citations |
| tavily | `TAVILY_API_KEY` | LLM-friendly, free tier 1K/mo |
| ddg | — | Public; no key. Polite rate. |
| hn | — | Algolia HN Search is public |
| github | `GITHUB_TOKEN` | Reuses the fetcher token; 30 req/min authenticated |

Plus:

| Env | Default | Purpose |
|---|---|---|
| `SPIDER_SEARCH_FALLBACK_CHAIN` | `brave` | Default engine list when caller omits `engines=` |
| `SPIDER_SEARCH_TIMEOUT_S` | `10` | Per-engine timeout in dispatcher |

## Rate limiting

Per-engine `RateLimiter` instance (reuse `spider/evasion/rate.py`). Conservative defaults, override-able via `SPIDER_SEARCH_<ENGINE>_RPM` env:

| Engine | Default RPM |
|---|---|
| brave | 60 |
| linkup | 120 |
| perplexity | 60 |
| tavily | 60 |
| ddg | 30 (HTML scrape — be polite) |
| hn | 300 (Algolia is generous) |
| github | 60 |

Limiter is per-process; behind a multi-replica deployment we'd need a Redis-backed limiter — out of scope for v0.6.0.

## RRF merge algorithm

For `mode="merge"`, parallel-fan-out to N engines, then reciprocal-rank-fusion combine:

```python
def rrf_merge(responses: list[SearchResponse], k: int = 60) -> list[SearchHit]:
    """Reciprocal Rank Fusion — Cormack et al., 2009. k=60 is standard."""
    scored: dict[str, float] = {}
    seen: dict[str, SearchHit] = {}
    for resp in responses:
        for hit in resp.results:
            scored[hit.url] = scored.get(hit.url, 0.0) + 1.0 / (k + hit.rank)
            # Keep the highest-ranked instance of this URL (lowest rank
            # number wins).
            existing = seen.get(hit.url)
            if existing is None or hit.rank < existing.rank:
                seen[hit.url] = hit
    return sorted(seen.values(), key=lambda h: -scored[h.url])
```

Each URL accumulates `1 / (k + rank)` from every engine that returned it. URLs in multiple engines' top-10 climb the merged list naturally. Standalone results from one engine still appear, just lower.

## Migration plan

| Phase | PR | Scope | LoC est. |
|---|---|---|---|
| **PR-B-1** | this PR | Skeleton: `__init__.py`, `base.py`, `models.py` stubs. No engines. Design doc. | ~200 LoC |
| **PR-B-2** | follow-up | Port `brave`, `linkup`, `perplexity` from web-search-agent. Wire `dispatch_search(mode="primary")`. HTTP route + skill. Tests with fakes. | ~600 LoC |
| **PR-B-3** | follow-up | New engines: `tavily`, `ddg`, `hn`, `github`. Per-engine tests. | ~500 LoC |
| **PR-B-4** | follow-up | `mode="merge"` path + RRF + parallel-fan-out. Tests for overlap/disjoint result sets. | ~250 LoC |
| **PR-E** | [VIB-30](https://linear.app/vibepeter/issue/VIB-30) | `web-search-agent` + `pplx-agent` become thin proxies → spider. After 1 release cycle, archive repos. | ~150 LoC + repo changes |

Total: ~1700 LoC over 5 PRs. PR-B-2 unblocks production usage; PR-B-3/4 are nice-to-haves.

## Risks + open questions

1. **API key relocation.** Keys today live in `web-search-agent`'s env. Moving to spider means deployment templates (`deploy/k3s/secret.example.yaml`, docker-compose `.env`) need to gain BRAVE/LINKUP/PERPLEXITY/TAVILY entries. Document the migration in PR-B-2's CHANGELOG note.

2. **Quota visibility.** With `mode="merge"`, one user query hits N engines. Costs go up linearly — Brave free tier (~2K/mo) could be exhausted in a single Linear bug-triage workflow. Mitigation: per-engine daily-budget tracker in Redis; `mode="merge"` is opt-in only and surfaces "would exceed budget" as a soft error.

3. **DDG result quality.** DDG's instant answer API is limited; full SERP results require HTML scraping. Scraping is brittle and Cloudflare-protected. Recommendation: use DDG only as fallback-of-last-resort.

4. **Perplexity returns synthesized content, not just links.** The current adapter likely extracts citations. Decision: keep that behavior (return citations as `SearchHit`s) and add an optional `synthesis: str | None` field to `SearchResponse` for the answer text. Non-perplexity engines leave it None.

5. **GitHub code search returns code snippets.** Different result shape (file path + line range + snippet). Two options: (a) shoehorn into `SearchHit` with a `code_snippet` field; (b) separate `CodeSearchHit` subtype. I lean (a) — keeps the dispatcher single-shaped; the URL points to the file on GitHub.

6. **In-flight cancellation in merge mode.** When one engine times out at 10s, do we cancel the others or wait? Recommendation: `asyncio.wait(tasks, timeout=timeout_s, return_when=ALL_COMPLETED)` — wait for everyone or hit global timeout, then cancel stragglers. Returns whatever finished.

7. **`depth` parameter.** Today `depth: Literal["standard", "deep"]` is mostly a Linkup-specific knob. Engines that don't support it ignore it. Keep the parameter, document per-engine semantics in each module's docstring. Better than introducing engine-specific parameter dicts that callers have to remember.

## Acceptance for VIB-29 (this design)

- [x] Design doc lands at `docs/search-consolidation.md`
- [x] Mirrored Linear document attached to the project
- [x] Stub `spider/search/` skeleton: `__init__.py`, `models.py`, `base.py`
- [x] No engine implementations — those are PR-B-2+
- [x] No HTTP route or skill yet — those are PR-B-2+

## Files this PR adds

- `docs/search-consolidation.md` (this file)
- `spider/search/__init__.py` (stub)
- `spider/search/base.py` (Protocol)
- `spider/search/models.py` (types)
