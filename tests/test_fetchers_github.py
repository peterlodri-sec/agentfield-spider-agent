"""GitHub repo + issue fetchers.

Uses a fake ``httpx.AsyncClient`` so the tests never hit the network.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from spider.fetchers import github


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict | None = None,
        *,
        raise_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self._raise_json = raise_json
        self.text = (
            "<not-json>" if raise_json else json.dumps(self._body)
        )
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class _FakeClient:
    """A FakeClient is constructed with a dict of URL-substring → response.

    The github fetcher makes 1-2 sequential GET calls per fetch:
      GET /repos/{o}/{r}        — repo metadata
      GET /repos/{o}/{r}/readme — base64-encoded README
    For each call we look up the response by substring match on the
    requested URL.
    """

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append((url, kw))
        for sub, resp in self.routes.items():
            if sub in url:
                return resp
        raise AssertionError(f"no route for url: {url}")


def _patch_httpx(monkeypatch, routes: dict[str, _FakeResponse]) -> _FakeClient:
    fake = _FakeClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


def _readme_b64(content: str) -> dict:
    return {
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


# ─── fetch_repo ──────────────────────────────────────────────────────
def test_fetch_repo_happy_path(monkeypatch) -> None:
    fake = _patch_httpx(monkeypatch, {
        "/repos/zakirullin/files.md/readme": _FakeResponse(
            200,
            _readme_b64(
                "# files.md\n\n"
                "A simple application for your `.md` files. "
                "Private, no data is sent to server.\n"
            ),
        ),
        "/repos/zakirullin/files.md": _FakeResponse(200, {
            "description": "🌱 Your life in plain .md files",
            "stargazers_count": 1234,
            "forks_count": 56,
            "language": "Go",
            "default_branch": "main",
            "license": {"spdx_id": "MIT"},
            "archived": False,
            "open_issues_count": 7,
        }),
    })

    out = _run(github.fetch_repo("https://github.com/zakirullin/files.md"))

    assert out["status"] == "ok"
    assert out["kind"] == "github-repo"
    assert out["name"] == "zakirullin/files.md"
    assert out["title"] == "zakirullin/files.md"
    assert out["url"] == "https://github.com/zakirullin/files.md"
    assert "Your life in plain" in out["description"]
    assert out["text"] is not None
    assert "Private, no data" in out["text"]
    assert out["metadata"]["stars"] == 1234
    assert out["metadata"]["license"] == "MIT"
    assert out["metadata"]["language"] == "Go"
    # Exactly two API calls.
    assert len(fake.calls) == 2
    assert "/repos/zakirullin/files.md" in fake.calls[0][0]


def test_fetch_repo_strips_dot_git_suffix(monkeypatch) -> None:
    _patch_httpx(monkeypatch, {
        "/repos/o/r/readme": _FakeResponse(200, _readme_b64("ok")),
        "/repos/o/r": _FakeResponse(200, {"description": "x"}),
    })
    out = _run(github.fetch_repo("https://github.com/o/r.git"))
    assert out["url"] == "https://github.com/o/r"
    assert out["name"] == "o/r"


def test_fetch_repo_uses_topics_when_description_empty(monkeypatch) -> None:
    _patch_httpx(monkeypatch, {
        "/repos/o/r/readme": _FakeResponse(200, _readme_b64("ok")),
        "/repos/o/r": _FakeResponse(200, {
            "description": "",
            "topics": ["rust", "async", "crawler"],
        }),
    })
    out = _run(github.fetch_repo("https://github.com/o/r"))
    assert out["description"].startswith("topics: ")
    assert "rust" in out["description"]


def test_fetch_repo_missing_readme_still_returns_ok(monkeypatch) -> None:
    """Repo without a README returns ok with text=None, no fatal error."""
    _patch_httpx(monkeypatch, {
        "/repos/o/r/readme": _FakeResponse(404),
        "/repos/o/r": _FakeResponse(200, {"description": "stub repo"}),
    })
    out = _run(github.fetch_repo("https://github.com/o/r"))
    assert out["status"] == "ok"
    assert out["text"] is None


def test_fetch_repo_404_returns_error(monkeypatch) -> None:
    """If the repo itself doesn't exist, surface a non-ok result."""
    _patch_httpx(monkeypatch, {
        "/repos/ghost/repo": _FakeResponse(404),
        "/repos/ghost/repo/readme": _FakeResponse(404),
    })
    out = _run(github.fetch_repo("https://github.com/ghost/repo"))
    assert out["status"] == "error"
    assert "github repo api failed" in out["error"]


def test_fetch_repo_rejects_bad_url() -> None:
    out = _run(github.fetch_repo("not a url"))
    assert out["status"] == "error"
    assert "not a github repo url" in out["error"]


def test_fetch_repo_auth_header_present_when_token_set(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret123")
    fake = _patch_httpx(monkeypatch, {
        "/repos/o/r/readme": _FakeResponse(200, _readme_b64("ok")),
        "/repos/o/r": _FakeResponse(200, {"description": "ok"}),
    })
    _run(github.fetch_repo("https://github.com/o/r"))
    # GET kwargs include the Authorization header.
    first_call_kwargs = fake.calls[0][1]
    assert first_call_kwargs["headers"]["Authorization"] == "Bearer secret123"


def test_fetch_repo_no_auth_header_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake = _patch_httpx(monkeypatch, {
        "/repos/o/r/readme": _FakeResponse(200, _readme_b64("ok")),
        "/repos/o/r": _FakeResponse(200, {"description": "ok"}),
    })
    _run(github.fetch_repo("https://github.com/o/r"))
    first_call_kwargs = fake.calls[0][1]
    assert "Authorization" not in first_call_kwargs["headers"]


# ─── fetch_issue ─────────────────────────────────────────────────────
def test_fetch_issue_happy_path(monkeypatch) -> None:
    fake = _patch_httpx(monkeypatch, {
        "/repos/devlikeapro/waha/issues/1076": _FakeResponse(200, {
            "title": "💵 Bug Bounty – $2,100 if You Can Bypass WAHA API Key Validation",
            "state": "open",
            "labels": [{"name": "security"}, {"name": "bounty"}],
            "user": {"login": "devlikeapro"},
            "comments": 12,
            "body": "## Background\n\nWAHA validates API keys via X-Api-Key header.",
        }),
    })

    out = _run(github.fetch_issue(
        "https://github.com/devlikeapro/waha/issues/1076",
    ))

    assert out["status"] == "ok"
    assert out["kind"] == "github-issue"
    assert "Bug Bounty" in out["title"]
    assert "devlikeapro/waha#1076" in out["title"]
    assert out["metadata"]["state"] == "open"
    assert out["metadata"]["labels"] == ["security", "bounty"]
    assert out["metadata"]["user"] == "devlikeapro"
    assert "security" in out["description"]
    assert out["text"] is not None
    assert len(fake.calls) == 1


def test_fetch_issue_404_returns_error(monkeypatch) -> None:
    _patch_httpx(monkeypatch, {
        "/repos/o/r/issues/999": _FakeResponse(404),
    })
    out = _run(github.fetch_issue("https://github.com/o/r/issues/999"))
    assert out["status"] == "error"
    assert "github issue api failed" in out["error"]


def test_fetch_issue_rejects_bad_url() -> None:
    out = _run(github.fetch_issue("https://github.com/o/r"))
    assert out["status"] == "error"
    assert "not a github issue url" in out["error"]
