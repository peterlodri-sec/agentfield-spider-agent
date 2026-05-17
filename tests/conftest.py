"""Shared pytest fixtures.

The fakes here mimic just enough of CloakBrowser / aiohttp.ClientSession
to drive the v1 fetch pipeline without any network or browser process.
The real implementations are exercised in dev / smoke tests; in CI we
keep tests deterministic and offline.
"""
from __future__ import annotations

import contextlib
from typing import Any

import pytest


# ─── fake page (CloakBrowser / Playwright surface) ───────────────────
class FakePage:
    """Records calls + returns canned ``page.evaluate`` responses.

    The ``evaluate_responses`` dict maps a marker substring to the
    value to return when the page receives an ``evaluate(js, ...)``
    call whose ``js`` contains that marker. Unknown JS returns ``None``.
    """

    def __init__(
        self,
        *,
        evaluate_responses: dict[str, Any] | None = None,
        goto_should_fail: bool = False,
    ) -> None:
        self.evaluate_responses = evaluate_responses or {}
        self.goto_should_fail = goto_should_fail
        self.goto_calls: list[tuple[str, dict]] = []
        self.evaluate_calls: list[tuple[str, tuple]] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self.goto_should_fail:
            raise RuntimeError(f"FakePage.goto: simulated failure for {url}")
        self.goto_calls.append((url, kwargs))

    async def evaluate(self, js: str, *args: Any) -> Any:
        self.evaluate_calls.append((js, args))
        for marker, response in self.evaluate_responses.items():
            if marker in js:
                return response
        return None

    async def content(self) -> str:
        return "<html><body>fake</body></html>"

    async def close(self) -> None:
        self.closed = True


# ─── fake browser pool (matches CloakBrowserPool's .lease() contract) ─
class FakePool:
    """Substitute for ``spider.browser.CloakBrowserPool`` in tests.

    The handler at ``request.app['deps'].pool`` is duck-typed: anything
    with ``async lease()`` (asynccontextmanager) yielding a page is fine.
    """

    def __init__(self, page: FakePage) -> None:
        self._page = page
        self._browser: Any = None  # mirrors the real attribute for /health
        self.shutdown_called = False

    @contextlib.asynccontextmanager
    async def lease(self):
        yield self._page

    async def shutdown(self) -> None:
        self.shutdown_called = True


# ─── fake aiohttp.ClientSession ───────────────────────────────────────
class FakeResponse:
    """Just enough of an aiohttp response to satisfy ``follow_safe_redirects``."""

    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeHttp:
    """Returns the same canned response for every ``.get(url, ...)``."""

    def __init__(self, response: FakeResponse | None = None) -> None:
        self._response = response or FakeResponse(200)
        self.get_calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        return self._response

    async def close(self) -> None:
        return None


# ─── factory fixtures ────────────────────────────────────────────────
@pytest.fixture
def make_page():
    """Return a builder that produces ``FakePage`` instances."""

    def _make(
        *,
        title: str = "Example Title",
        text: str | None = None,
        cookie_match: bool = False,
        goto_should_fail: bool = False,
    ) -> FakePage:
        if text is None:
            text = "A long-enough body. " * 30  # > 200 chars to pass the gate
        responses: dict[str, Any] = {
            # extract.py's title+text JS contains 'og:title'; key off that
            # to hand back our canned values.
            "og:title": {"title": title, "text": text},
            # structured-data JS contains 'jsonld'
            "jsonld": {
                "jsonld": [],
                "microdata": [],
                "opengraph": {},
                "links": [],
                "images": [],
            },
        }
        # If cookie_match is True, the _TRY_CLICK_JS evaluate returns a
        # label string ('_' for selector-only matches in extract.py).
        if cookie_match:
            responses["const sel = args.selector"] = "_"
        else:
            # Fallback removal counts as "0 removed".
            responses["[id*=\"cookie\"]"] = 0
        return FakePage(
            evaluate_responses=responses,
            goto_should_fail=goto_should_fail,
        )

    return _make


@pytest.fixture
def make_deps(make_page, monkeypatch):
    """Build a ``FetchDeps`` with fake pool + fake http.

    Also patches the politeness module to disable robots checks and
    rate-limit waits — tests don't need to wait for token bucket
    refills.
    """
    from spider import politeness
    from spider.fetch import FetchDeps

    monkeypatch.setattr(politeness, "ROBOTS_DISABLE", True)
    monkeypatch.setattr(politeness, "HOST_RPS", 0.0)

    def _make(page: FakePage | None = None) -> tuple[FetchDeps, FakePage]:
        p = page or make_page()
        http = FakeHttp(FakeResponse(200))
        pool = FakePool(p)
        robots = politeness.RobotsCache(http)  # type: ignore[arg-type]
        bucket = politeness.HostBucket()
        return FetchDeps(
            http=http,  # type: ignore[arg-type]
            pool=pool,  # type: ignore[arg-type]
            robots=robots,
            bucket=bucket,
        ), p

    return _make
