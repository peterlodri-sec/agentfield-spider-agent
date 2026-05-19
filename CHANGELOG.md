# agentfield-spider-agent CHANGELOG

Format based on [Keep a Changelog](https://keepachangelog.com/). Versions track `pyproject.toml`.

The canonical project changelog lives as a [Linear document](https://linear.app/vibepeter/document/changelog-e9ae45b90626); this file mirrors it for users browsing the repo on GitHub.

---

## [Unreleased]

### Planned (v0.5.0)

- `BrowserUsePool` + `POST /act` (browser-use sidecar)
- Crawl worker background loop
- AgentField skills for sessions + browser_act

---

## [0.4.0] — 2026-05-19

### Added

- **EvasionBase layer** (`spider/evasion/`) — composable bot-detection countermeasures — PR #11
  - 5 persona presets (`desktop-us-en`, `desktop-de-de`, `desktop-macos-en`, `mobile-android-en`, `mobile-ios-en`) pinning UA + locale + timezone + viewport + geolocation + Sec-CH-UA as a coherent identity
  - WebRTC IP-leak block init script (Lightpanda-only; Cloak handles its own at source level)
  - Per-session token-bucket rate limiter (`RateLimiter`)
  - Post-goto behavioral simulation (dwell + scroll + mouse jitter), backend-aware
- **Profile round-trip** via `SessionManager` ↔ `ProfileStore` — commit `68ca945`
  - `POST /sessions {"profile": "<name>"}` hydrates the new context from postgres `storage_state` (VIB-13)
  - `DELETE /sessions/{id}?persist_profile=<name>` snapshots `context.storage_state()` and upserts to postgres before close (VIB-14)
  - `SessionManager.export_storage_state(session_id)` accessor
- **`POST /sessions`** accepts `persona` + `evasion_rate_per_min` + `profile` body fields
- **CI / build**:
  - Docker job builds via Docker Buildx Cloud and pushes to Docker Hub on push-to-main; build-only on PRs (PR #18)
  - Multi-stage Dockerfile on Docker Hardened Images (`dhi.io/python:3.12-dev`); `BASE_BUILDER` / `BASE_RUNTIME` ARG-overridable (PR #44)

### Changed

- `SessionManager.create()` accepts optional `persona`, `evasion_rate_per_min`, `profile` kwargs; persona-derived `context_kwargs` are merged BEFORE explicit caller kwargs (explicit wins)
- `fetch._fetch_one_core()` calls `evasion.check_rate()` before `page.goto` and `evasion.post_goto()` after, when the session has evasion attached

### Internal

- `scripts/claude-routine/` — Linear → PR dispatcher + status monitor for fleet-wide agent coordination (PR #19)

---

## [0.3.0] — 2026-05-18

### Added

- Postgres **profiles** (`/profiles`, storage_state + user_data_zip) — PR #8
- **Domain errors** (`spider/errors.py`) + `outcome_from_exception` — PR #10
- `/chrome-conn` and `/vnc-conn` for browser-use sidecar discovery
- GitHub CI: ruff + mypy + pytest via uv — PR #10

### Changed

- Ruff clean across codebase (108 violations fixed) — PR #9
- `FetchDeps.pool` typed as `BrowserPool` protocol
- Typed fetch errors in `fetch_one` (`SSRFBlockedError`, `RobotsBlockedError`, etc.)

### Fixed

- Profile multipart upload mypy (BodyPartReader guard)
- `browser_use_engine` absolute imports

---

## Versioning policy

| Bump | When |
| -- | -- |
| **patch** | Bugfix, deps, docs-only |
| **minor** | New API fields, backends, non-breaking features |
| **major** | Breaking HTTP/skill contracts, Redis key migrations |

Milestones in Linear map 1:1 to minor releases.
