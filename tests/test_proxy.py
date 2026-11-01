"""Outbound proxy resolution.

env_proxy() reads SPIDER_PROXY_URL; resolve_proxy() returns the
per-call override if given, else the env, else None. Empty strings
count as 'bypass'.
"""

from __future__ import annotations

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


def test_cloakbrowser_launch_kwargs_extracts_inline_credentials(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://alice:s3cr%40t@phone.local:8888")
    assert proxy.cloakbrowser_launch_kwargs() == {
        "proxy": {
            "server": "http://phone.local:8888",
            "username": "alice",
            "password": "s3cr@t",
        }
    }


def test_cloakbrowser_launch_kwargs_stealth_env_knobs(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    monkeypatch.setenv("CLOAK_HUMANIZE", "1")
    monkeypatch.setenv("CLOAK_HUMAN_PRESET", "careful")
    monkeypatch.setenv("CLOAK_GEOIP", "true")
    monkeypatch.setenv("CLOAK_TIMEZONE", "Atlantic/Reykjavik")
    monkeypatch.setenv("CLOAK_LOCALE", "en-IS")
    assert proxy.cloakbrowser_launch_kwargs() == {
        "humanize": True,
        "human_preset": "careful",
        "geoip": True,
        "timezone": "Atlantic/Reykjavik",
        "locale": "en-IS",
    }


# --- SPIDER_PROXY_TRUST_INSECURE -----------------------------------------


def test_trust_insecure_unset(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_TRUST_INSECURE", raising=False)
    assert proxy.trust_insecure() is False


def test_trust_insecure_empty(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "")
    assert proxy.trust_insecure() is False


def test_trust_insecure_truthy_values(monkeypatch) -> None:
    for v in ("1", "true", "TRUE", "Yes", "yes", "on", "ON"):
        monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", v)
        assert proxy.trust_insecure() is True, f"expected truthy: {v!r}"


def test_trust_insecure_falsy_values(monkeypatch) -> None:
    for v in ("0", "false", "no", "off", "anything-else", "  ", "FALSE"):
        monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", v)
        assert proxy.trust_insecure() is False, f"expected falsy: {v!r}"


def test_trust_insecure_whitespace_trimmed(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "  yes  ")
    assert proxy.trust_insecure() is True


def test_httpx_client_kwargs_adds_verify_false_when_insecure(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "1")
    assert proxy.httpx_client_kwargs() == {"proxy": "http://env:8080", "verify": False}


def test_httpx_client_kwargs_verify_only_when_insecure_no_proxy(monkeypatch) -> None:
    """verify=False is added even without a proxy when the flag is set —
    callers shouldn't be silently strict if the operator explicitly opted in."""
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "1")
    assert proxy.httpx_client_kwargs() == {"verify": False}


def test_httpx_client_kwargs_no_verify_when_secure(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    monkeypatch.delenv("SPIDER_PROXY_TRUST_INSECURE", raising=False)
    assert proxy.httpx_client_kwargs() == {"proxy": "http://env:8080"}


def test_aiohttp_request_kwargs_adds_ssl_false_when_insecure(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_PROXY_URL", "http://env:8080")
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "1")
    assert proxy.aiohttp_request_kwargs() == {"proxy": "http://env:8080", "ssl": False}


def test_aiohttp_request_kwargs_ssl_only_when_insecure_no_proxy(monkeypatch) -> None:
    monkeypatch.delenv("SPIDER_PROXY_URL", raising=False)
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "1")
    assert proxy.aiohttp_request_kwargs() == {"ssl": False}


def test_insecure_works_with_per_call_proxy(monkeypatch) -> None:
    """Per-call proxy + insecure flag = override applies, ssl still bypassed."""
    monkeypatch.setenv("SPIDER_PROXY_TRUST_INSECURE", "1")
    out = proxy.httpx_client_kwargs("http://per:1234")
    assert out == {"proxy": "http://per:1234", "verify": False}
