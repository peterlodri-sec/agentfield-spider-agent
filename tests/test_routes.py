"""Route handler tests.

Builds a minimal aiohttp application via ``spider.routes.register_routes``
+ pre-installed fake deps. This bypasses ``spider_server.build_app``'s
startup hook (which would create a real CloakBrowserPool) and runs each
test fully offline.
"""
from __future__ import annotations

import asyncio
from typing import Any

import orjson
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from spider.routes import register_routes


def _build_test_app(deps: Any) -> web.Application:
    app = web.Application()
    app["http"] = deps.http
    app["browser"] = deps.pool
    app["robots"] = deps.robots
    app["bucket"] = deps.bucket
    app["deps"] = deps
    register_routes(app)
    return app


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_health_returns_browser_state(make_deps) -> None:
    deps, _page = make_deps()
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                r = await client.get("/health")
                assert r.status == 200
                body = await r.json()
                assert body["ok"] is True
                assert body["service"] == "spider-http"
                assert body["browser"] in ("open", "closed", "absent")

    _run(_go())


def test_fetch_sync_rejects_bad_body(make_deps) -> None:
    deps, _ = make_deps()
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                # No body at all
                r = await client.post("/fetch/sync", data="")
                assert r.status == 400
                # Body without url
                r = await client.post(
                    "/fetch/sync",
                    data=orjson.dumps({}),
                    headers={"Content-Type": "application/json"},
                )
                assert r.status == 400

    _run(_go())


def test_fetch_sync_blocks_private_ip_at_layer1(make_deps) -> None:
    deps, _ = make_deps()
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                r = await client.post(
                    "/fetch/sync",
                    data=orjson.dumps({"url": "http://127.0.0.1/secret"}),
                    headers={"Content-Type": "application/json"},
                )
                assert r.status == 400
                body = await r.json()
                assert "SSRF" in body.get("error", "")

    _run(_go())


def test_fetch_sync_happy_path_returns_ok(make_deps, monkeypatch) -> None:
    """End-to-end fetch with all gates monkeypatched to allow + fake page."""
    # Bypass DNS-based SSRF + redirect walk so we never make network calls.
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(
        sess: Any, url: str, **kwargs: Any,
    ) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(
        fetch, "is_safe_external_url_with_dns", _allow_all,
    )
    monkeypatch.setattr(
        fetch, "follow_safe_redirects", _identity_redirect,
    )

    deps, page = make_deps()
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                r = await client.post(
                    "/fetch/sync",
                    data=orjson.dumps({"url": "https://example.com/foo"}),
                    headers={"Content-Type": "application/json"},
                )
                assert r.status == 200
                body = await r.json()
                assert body["status"] == "ok"
                assert body["title"] == "Example Title"
                assert "fetch_url" in body
                assert body["ms"] >= 0
        # The FakePage's goto was called with the input URL.
        assert page.goto_calls and page.goto_calls[0][0] == (
            "https://example.com/foo"
        )

    _run(_go())


def test_fetch_sync_returns_204_on_empty_content(
    make_deps, make_page, monkeypatch,
) -> None:
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(
        sess: Any, url: str, **kwargs: Any,
    ) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(
        fetch, "is_safe_external_url_with_dns", _allow_all,
    )
    monkeypatch.setattr(
        fetch, "follow_safe_redirects", _identity_redirect,
    )

    page = make_page(text="too short")  # < 100 chars triggers empty
    deps, _ = make_deps(page)
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                r = await client.post(
                    "/fetch/sync",
                    data=orjson.dumps({"url": "https://example.com/"}),
                    headers={"Content-Type": "application/json"},
                )
                # 204 = no body; aiohttp's client surfaces it as such.
                assert r.status == 204

    _run(_go())


def test_fetch_sync_returns_502_on_browser_error(
    make_deps, make_page, monkeypatch,
) -> None:
    from spider import fetch

    async def _allow_all(url: str) -> bool:  # noqa: ARG001
        return True

    async def _identity_redirect(
        sess: Any, url: str, **kwargs: Any,
    ) -> str:  # noqa: ARG001
        return url

    monkeypatch.setattr(
        fetch, "is_safe_external_url_with_dns", _allow_all,
    )
    monkeypatch.setattr(
        fetch, "follow_safe_redirects", _identity_redirect,
    )

    page = make_page(goto_should_fail=True)
    deps, _ = make_deps(page)
    app = _build_test_app(deps)

    async def _go() -> None:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                r = await client.post(
                    "/fetch/sync",
                    data=orjson.dumps({"url": "https://example.com/"}),
                    headers={"Content-Type": "application/json"},
                )
                assert r.status == 502
                body = await r.json()
                assert body["status"] == "error"
                assert "FakePage.goto" in body["error"]

    _run(_go())
