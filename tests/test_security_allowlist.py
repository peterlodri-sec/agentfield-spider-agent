"""Tests for the optional ``SPIDER_AGENT_ALLOWED_HOSTS`` allowlist
helpers used by ``POST /act``'s navigation gate.

Unit-only; no network. Covers ``_parse_host_list``, ``is_host_allowed``,
and ``env_agent_allowed_hosts``.
"""

from __future__ import annotations

import pytest

from spider.security import (
    _parse_host_list,
    env_agent_allowed_hosts,
    is_host_allowed,
)


def test_parse_host_list_empty_inputs() -> None:
    assert _parse_host_list(None) == frozenset()
    assert _parse_host_list("") == frozenset()
    assert _parse_host_list(" , , ") == frozenset()


def test_parse_host_list_normalizes() -> None:
    got = _parse_host_list("Example.com, .Docs.Python.Org , api.github.com.")
    assert got == frozenset(
        {
            "example.com",
            ".docs.python.org",
            "api.github.com",
        }
    )


def test_is_host_allowed_no_allowlist_passes_everything() -> None:
    assert is_host_allowed("https://example.com/x", None) is True
    assert is_host_allowed("https://example.com/x", frozenset()) is True


def test_is_host_allowed_exact_match() -> None:
    allowed = frozenset({"example.com"})
    assert is_host_allowed("https://example.com/x", allowed) is True
    assert is_host_allowed("https://EXAMPLE.com/x", allowed) is True
    assert is_host_allowed("https://other.com/x", allowed) is False
    # Subdomains do NOT match a bare entry.
    assert is_host_allowed("https://api.example.com/x", allowed) is False


def test_is_host_allowed_subdomain_suffix() -> None:
    allowed = frozenset({".example.com"})
    # The bare domain matches.
    assert is_host_allowed("https://example.com/x", allowed) is True
    # Subdomains match.
    assert is_host_allowed("https://api.example.com/x", allowed) is True
    assert is_host_allowed("https://a.b.example.com/", allowed) is True
    # A different domain that happens to end in "example.com" via a
    # confused suffix doesn't match (we require the dot boundary).
    assert is_host_allowed("https://notexample.com/x", allowed) is False


def test_is_host_allowed_rejects_unparseable() -> None:
    allowed = frozenset({"example.com"})
    assert is_host_allowed("not a url", allowed) is False
    assert is_host_allowed("https:///path", allowed) is False


def test_env_agent_allowed_hosts_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SPIDER_AGENT_ALLOWED_HOSTS",
        "example.com, .docs.python.org , api.github.com",
    )
    got = env_agent_allowed_hosts()
    assert got == frozenset(
        {
            "example.com",
            ".docs.python.org",
            "api.github.com",
        }
    )


def test_env_agent_allowed_hosts_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIDER_AGENT_ALLOWED_HOSTS", raising=False)
    assert env_agent_allowed_hosts() == frozenset()
