"""SSRF gate sanity checks. Layer-1 only — no DNS dependency."""
from __future__ import annotations

import pytest

from spider.security import (
    is_safe_external_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://example.com/path?q=1",
        "https://github.com/foo/bar",
        "https://1.1.1.1/",  # public IP literal
    ],
)
def test_layer1_allows_public_urls(url: str) -> None:
    assert is_safe_external_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 of private IP
        # Legacy IPv4 forms that ``ipaddress`` won't reject but
        # getaddrinfo / inet_aton will resolve to loopback:
        "http://2130706433/",  # uint32 form of 127.0.0.1
        "http://0177.0.0.1/",  # octal form
        # Fleet service hostnames the spider must never connect to:
        "http://control-plane/",
        "http://spider-agent/",
        "http://redis/",
    ],
)
def test_layer1_blocks_private_and_internal(url: str) -> None:
    assert is_safe_external_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "",
        "not a url at all",
        "https://",
    ],
)
def test_layer1_blocks_bad_schemes_and_garbage(url: str) -> None:
    assert is_safe_external_url(url) is False
