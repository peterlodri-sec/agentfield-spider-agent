"""Outbound proxy resolution.

env_proxy() reads SPIDER_PROXY_URL; resolve_proxy() returns the
per-call override if given, else the env, else None. Empty strings
count as 'bypass'.
"""
from __future__ import annotations

import pytest

from spider import proxy


def test_env_proxy_unset(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    assert proxy.env_proxy() is None


def test_env_proxy_set(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "socks5://10.0.0.1:1080")
    assert proxy.env_proxy() == "socks5://10.0.0.1:1080"


def test_env_proxy_whitespace_trimmed(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "  http://x:8080  ")
    assert proxy.env_proxy() == "http://x:8080"


def test_env_proxy_empty_string(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "")
    assert proxy.env_proxy() is None


def test_resolve_proxy_per_call_wins(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.resolve_proxy("http://per-call:9090") == "http://per-call:9090"


def test_resolve_proxy_per_call_empty_bypasses_env(monkeypatch) -> None:
    """Pass `proxy=""` to forcibly bypass the env-configured proxy for one call."""
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.resolve_proxy("") is None


def test_resolve_proxy_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.resolve_proxy(None) == "http://env:8080"


def test_resolve_proxy_no_env_no_per_call(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    assert proxy.resolve_proxy(None) is None


def test_aiohttp_request_kwargs_proxied(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.aiohttp_request_kwargs() == {"proxy": "http://env:8080"}


def test_aiohttp_request_kwargs_no_proxy(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    assert proxy.aiohttp_request_kwargs() == {}


def test_aiohttp_request_kwargs_per_call_override(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.aiohttp_request_kwargs("http://per:1") == {"proxy": "http://per:1"}


def test_httpx_client_kwargs_proxied(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    assert proxy.httpx_client_kwargs() == {"proxy": "http://env:8080"}


def test_httpx_client_kwargs_no_proxy(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    assert proxy.httpx_client_kwargs() == {}


def test_cloakbrowser_launch_kwargs_playwright_shape(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    kwargs = proxy.cloakbrowser_launch_kwargs()
    assert kwargs == {"proxy": {"server": "http://env:8080"}}


def test_cloakbrowser_launch_kwargs_no_proxy(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    assert proxy.cloakbrowser_launch_kwargs() == {}
