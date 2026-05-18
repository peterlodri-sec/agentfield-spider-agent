"""Route tests for profile ↔ session integration (VIB-13 / VIB-14)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import orjson
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from spider.routes import register_routes
from spider.sessions import SessionManager
from tests.test_sessions import _FakePool as SessionFakePool


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeProfiles:
    """Minimal ProfileStore stand-in for route tests."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.enabled = enabled
        self._states = states or {}
        self.touched: list[str] = []
        self.upserts: list[tuple[str, dict[str, Any]]] = []

    async def fetch_storage_state(self, name: str) -> dict[str, Any] | None:
        return self._states.get(name)

    async def touch(self, name: str) -> None:
        self.touched.append(name)

    async def upsert(
        self,
        *,
        name: str,
        backend: str = "cloak",
        storage_state: dict[str, Any] | None = None,
        user_data_zip: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        self.upserts.append((name, dict(storage_state or {})))
        return None


def _deps_for_sessions(make_deps: Any) -> Any:
    deps, _page = make_deps()
    return replace(deps, pool=SessionFakePool())  # type: ignore[arg-type]


def _build_app(deps: Any, *, profiles: _FakeProfiles | None = None) -> web.Application:
    app = web.Application()
    app["http"] = deps.http
    app["browser"] = deps.pool
    app["robots"] = deps.robots
    app["bucket"] = deps.bucket
    app["deps"] = deps
    app["sessions"] = SessionManager(max_sessions=10, ttl_s=3600)
    if profiles is not None:
        app["profiles"] = profiles
    register_routes(app)
    return app


def test_post_sessions_hydrates_from_profile(make_deps) -> None:
    deps = _deps_for_sessions(make_deps)
    profiles = _FakeProfiles(states={"alice": {"cookies": [{"name": "s", "value": "1"}]}})
    app = _build_app(deps, profiles=profiles)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await client.post(
                "/sessions",
                data=orjson.dumps({"profile": "alice"}),
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 200
            body = await r.json()
            assert body["id"]
            assert profiles.touched == ["alice"]

    _run(_go())


def test_post_sessions_unknown_profile_404(make_deps) -> None:
    deps = _deps_for_sessions(make_deps)
    profiles = _FakeProfiles(states={})
    app = _build_app(deps, profiles=profiles)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await client.post(
                "/sessions",
                data=orjson.dumps({"profile": "missing"}),
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 404

    _run(_go())


def test_post_sessions_profile_503_when_disabled(make_deps) -> None:
    deps = _deps_for_sessions(make_deps)
    profiles = _FakeProfiles(enabled=False)
    app = _build_app(deps, profiles=profiles)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await client.post(
                "/sessions",
                data=orjson.dumps({"profile": "alice"}),
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 503

    _run(_go())


def test_delete_sessions_persist_profile(make_deps) -> None:
    deps = _deps_for_sessions(make_deps)
    profiles = _FakeProfiles()
    app = _build_app(deps, profiles=profiles)

    async def _go() -> None:
        async with TestServer(app) as server, TestClient(server) as client:
            r = await client.post("/sessions", data=orjson.dumps({}))
            sid = (await r.json())["id"]
            r = await client.delete(f"/sessions/{sid}?persist_profile=bob")
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
            assert body["persisted_profile"] == "bob"
            assert profiles.upserts == [("bob", {"cookies": [], "origins": []})]

    _run(_go())
