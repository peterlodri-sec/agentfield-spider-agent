"""Cookie-banner dismissal + DOM extraction tests.

We don't need a real browser — the extract module is a thin wrapper
around ``page.evaluate(js, ...)``. The fixtures in ``conftest.py``
provide a ``FakePage`` that returns canned responses keyed by marker
substrings in the JS source.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spider.extract import (
    dismiss_banners, extract_structured, extract_title_text,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_extract_title_text_returns_canned(make_page) -> None:
    page = make_page(title="Hello", text="Body. " * 50)
    title, text = _run(extract_title_text(page))
    assert title == "Hello"
    assert "Body." in text


def test_extract_title_text_handles_empty_response(make_page) -> None:
    page = make_page(title="", text="")
    title, text = _run(extract_title_text(page))
    assert title == ""
    assert text == ""


def test_extract_structured_returns_shape(make_page) -> None:
    page = make_page()
    result = _run(extract_structured(page))
    assert set(result.keys()) >= {
        "jsonld", "microdata", "opengraph", "links", "images",
    }
    assert isinstance(result["jsonld"], list)
    assert isinstance(result["opengraph"], dict)


def test_dismiss_banners_returns_true_when_rule_matches(make_page) -> None:
    page = make_page(cookie_match=True)
    handled = _run(dismiss_banners(page))
    assert handled is True


def test_dismiss_banners_returns_false_when_no_rule_matches(make_page) -> None:
    page = make_page(cookie_match=False)
    handled = _run(dismiss_banners(page))
    assert handled is False
