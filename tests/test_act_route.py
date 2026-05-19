"""``POST /act`` route handler tests.

Covers:
  - body validation (missing task, missing session_id, bad max_steps, etc.)
  - 404 on unknown session
  - 501 when the [browser-use] extra isn't installed (default in CI)
  - happy path with browser_act monkeypatched to return a canned dict
  - allowlist plumbing (per-call + env merged) reaches the engine

Bypasses ``spider_server.build_app`` — no CloakBrowser, no Redis.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from spider import politeness
from spider.fetch import FetchDeps
from spider.routes import register_routes
from spider.sessions import SessionManager


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── fakes ──────────────────────────────────────────────────────────
class _FakePage:
    """Records the URLs handed to it; bare enough for SessionManager.lease_page."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx


class _FakePool:
    """Pool stub. SessionManager pokes ``_browser`` directly."""

    def __init__(self) -> None:
        self._browser = _FakeBrowser()


class _FakeHttp:
    async def close(self) -> None:
        return None


def _make_deps(pool: _FakePool, monkeypatch: Any) -> FetchDeps:
    """Build a FetchDeps wired up around the fake pool. Robots / RPS
    knobs disabled so tests don't hit politeness paths."""
    monkeypatch.setattr(politeness, "ROBOTS_DISABLE", True)
    monkeypatch.setattr(politeness, "HOST_RPS", 0.0)
    http = _FakeHttp()
    robots = politeness.RobotsCache(http)  # type: ignore[arg-type]
    bucket = politeness.HostBucket()
    return FetchDeps(
        http=http,  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        robots=robots,
        bucket=bucket,
    )


def _build_test_app(deps: FetchDeps, sessions: SessionManager) -> web.Application:
    app = web.Application()
    app["http"] = deps.http
    app["browser"] = deps.pool
    app["robots"] = deps.robots
    app["bucket"] = deps.bucket
    app["deps"] = deps
    app["sessions"] = sessions
    register_routes(app)
    return app


def _post_act(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        "/act",
        data=orjson.dumps(body),
        headers={"Content-Type": "application/json"},
    )


# ─── validation ─────────────────────────────────────────────────────
def test_act_rejects_missing_task(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()
    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(client, {"session_id": "abc"})
            assert r.status == 400
            body = await r.json()
            assert body["status"] == "error"
            assert "task" in body["error"]

    _run(_go())


def test_act_rejects_missing_session_id(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()
    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(client, {"task": "do the thing"})
            assert r.status == 400
            body = await r.json()
            assert "session_id" in body["error"]

    _run(_go())


def test_act_rejects_bad_max_steps(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()
    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            for bad in (0, -1, 101, "many", True):
                r = await _post_act(
                    client,
                    {"task": "x", "session_id": "abc", "max_steps": bad},
                )
                assert r.status == 400, f"max_steps={bad!r}"

    _run(_go())


def test_act_rejects_bad_allowed_hosts(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()
    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(
                client,
                {"task": "x", "session_id": "abc", "allowed_hosts": "not-a-list"},
            )
            assert r.status == 400
            r = await _post_act(
                client,
                {"task": "x", "session_id": "abc", "allowed_hosts": [123]},
            )
            assert r.status == 400

    _run(_go())


# ─── 404 / 503 ──────────────────────────────────────────────────────
def test_act_returns_404_on_unknown_session(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()
    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(client, {"task": "x", "session_id": "ghost"})
            assert r.status == 404
            body = await r.json()
            assert "not found" in body["error"]

    _run(_go())


def test_act_returns_503_without_sessions(monkeypatch) -> None:
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    app = web.Application()
    app["deps"] = deps
    register_routes(app)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(client, {"task": "x", "session_id": "abc"})
            assert r.status == 503

    _run(_go())


# ─── 501 (extra not installed) ──────────────────────────────────────
def test_act_returns_501_when_browser_use_extra_missing(monkeypatch) -> None:
    """With no [browser-use] extra installed, the engine returns a
    missing-dep envelope and the route maps it to 501."""
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()

    # Create a real session so we exercise the resolution path; the
    # engine fails on import before touching anything.
    async def _setup() -> str:
        info = await sessions.create(pool=pool, name="t")
        return info.id

    sid = _run(_setup())

    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(client, {"task": "x", "session_id": sid})
            assert r.status == 501, await r.text()
            body = await r.json()
            assert body["status"] == "error"
            assert "browser-use" in body["error"]

    _run(_go())


# ─── happy path with monkeypatched engine ───────────────────────────
def test_act_happy_path_with_mocked_engine(monkeypatch) -> None:
    """Patch ``browser_act`` to bypass the real Agent loop and verify
    the route returns the engine's flat dict verbatim."""
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()

    async def _setup() -> str:
        info = await sessions.create(pool=pool, name="happy")
        return info.id

    sid = _run(_setup())

    captured: dict[str, Any] = {}

    async def _fake_browser_act(
        task: str,
        *,
        session_id: str,
        sessions: Any,
        pool: Any,
        model: str | None = None,
        max_steps: int = 25,
        allowed_hosts: Any = None,
    ) -> dict[str, Any]:
        captured.update(
            task=task,
            session_id=session_id,
            model=model,
            max_steps=max_steps,
            allowed_hosts=allowed_hosts,
        )
        return {
            "status": "ok",
            "task": task,
            "session_id": session_id,
            "model": model or "openai/gpt-4o",
            "max_steps": max_steps,
            "ms": 42,
            "result": "the answer is 42",
            "urls_visited": ["https://example.com/"],
            "steps_taken": 3,
            "is_done": True,
        }

    monkeypatch.setattr(
        "spider.browsers.browser_use_engine.browser_act",
        _fake_browser_act,
    )

    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(
                client,
                {
                    "task": "  find the answer  ",
                    "session_id": sid,
                    "model": "anthropic/claude-3.5-sonnet",
                    "max_steps": 7,
                    "allowed_hosts": ["example.com", ".docs.example.com"],
                },
            )
            assert r.status == 200, await r.text()
            body = await r.json()
            assert body["status"] == "ok"
            assert body["result"] == "the answer is 42"
            assert body["ms"] == 42
            assert body["urls_visited"] == ["https://example.com/"]
            # task got stripped of whitespace by the handler.
            assert captured["task"] == "find the answer"
            assert captured["session_id"] == sid
            assert captured["model"] == "anthropic/claude-3.5-sonnet"
            assert captured["max_steps"] == 7
            # The handler should pass a non-None allowed_hosts when at
            # least one entry came in.
            assert captured["allowed_hosts"] is not None
            assert "example.com" in captured["allowed_hosts"]
            assert ".docs.example.com" in captured["allowed_hosts"]

    _run(_go())


def test_act_merges_env_allowlist_with_call(monkeypatch) -> None:
    """The env-level SPIDER_AGENT_ALLOWED_HOSTS is unioned with the
    per-call ``allowed_hosts`` body field."""
    monkeypatch.setenv(
        "SPIDER_AGENT_ALLOWED_HOSTS",
        "api.github.com, .openrouter.ai",
    )
    pool = _FakePool()
    deps = _make_deps(pool, monkeypatch)
    sessions = SessionManager()

    async def _setup() -> str:
        info = await sessions.create(pool=pool, name="env")
        return info.id

    sid = _run(_setup())

    captured: dict[str, Any] = {}

    async def _fake(task: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(allowed_hosts=kwargs.get("allowed_hosts"))
        return {"status": "ok", "task": task, "session_id": kwargs["session_id"]}

    monkeypatch.setattr(
        "spider.browsers.browser_use_engine.browser_act",
        _fake,
    )

    app = _build_test_app(deps, sessions)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await _post_act(
                client,
                {
                    "task": "x",
                    "session_id": sid,
                    "allowed_hosts": ["example.com"],
                },
            )
            assert r.status == 200
            allowed = captured["allowed_hosts"]
            assert allowed is not None
            assert "example.com" in allowed
            assert "api.github.com" in allowed
            assert ".openrouter.ai" in allowed

    _run(_go())
