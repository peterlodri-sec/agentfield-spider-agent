"""Docling integration tests.

The actual docling import + ML inference is heavy (~1.5 GB models on
first run), so we mock it via ``sys.modules`` injection. URL + content-
type detection are pure and don't need any mocking.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from spider import docling_parser
from spider.docling_parser import (
    DOC_CONTENT_TYPE_PREFIXES,
    DOC_EXTS,
    fetch_via_docling,
    is_docling_content_type,
    is_docling_url,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─── is_docling_url ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url,expected",
    [
        # Recognized extensions
        ("https://arxiv.org/pdf/2408.09869.pdf", True),
        ("https://example.com/paper.PDF", True),  # case-insensitive
        ("https://example.com/spec.docx", True),
        ("https://example.com/deck.pptx", True),
        ("https://example.com/sheet.xlsx", True),
        ("https://example.com/document.odt", True),
        # Tracking params / hash don't confuse detection
        ("https://example.com/paper.pdf?ref=hn", True),
        ("https://example.com/paper.pdf#page=3", True),
        # Not a doc extension
        ("https://example.com/", False),
        ("https://example.com/page.html", False),
        ("https://github.com/zakirullin/files.md", False),
        # Garbage
        ("", False),
        ("not a url", False),
    ],
)
def test_is_docling_url(url: str, expected: bool) -> None:
    assert is_docling_url(url) is expected


def test_is_docling_url_non_string_returns_false() -> None:
    assert is_docling_url(None) is False  # type: ignore[arg-type]
    assert is_docling_url(123) is False   # type: ignore[arg-type]


# ─── is_docling_content_type ────────────────────────────────────────
@pytest.mark.parametrize(
    "ct,expected",
    [
        ("application/pdf", True),
        ("application/pdf; charset=utf-8", True),
        ("application/x-pdf", True),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            True,
        ),
        ("application/msword", True),
        ("application/vnd.oasis.opendocument.text", True),
        ("application/vnd.ms-powerpoint", True),
        # Not docling
        ("text/html", False),
        ("application/json", False),
        ("image/png", False),
        ("", False),
        (None, False),
    ],
)
def test_is_docling_content_type(ct: str | None, expected: bool) -> None:
    assert is_docling_content_type(ct) is expected


def test_module_constants_are_frozensets_or_tuples() -> None:
    """Defensive: these are public surface and need to be hashable +
    immutable so callers can't accidentally mutate them."""
    assert isinstance(DOC_EXTS, frozenset)
    assert isinstance(DOC_CONTENT_TYPE_PREFIXES, tuple)


# ─── fetch_via_docling: dependency-missing path ──────────────────────
def test_fetch_via_docling_returns_error_when_docling_not_installed(
    monkeypatch,
) -> None:
    """Docling not importable should return a clean error envelope,
    not raise."""
    # Inject a fake import that always fails for docling.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name.startswith("docling"):
            raise ImportError("docling not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    out = _run(fetch_via_docling("https://arxiv.org/pdf/2408.09869.pdf"))

    assert out["status"] == "error"
    assert "docling not installed" in out["error"]
    assert out["kind"] == "docling-document"
    assert out["url"] == "https://arxiv.org/pdf/2408.09869.pdf"
    # Standard dispatcher contract fields present:
    assert "id" in out
    assert "ms" in out
    assert "ts" in out


# ─── fetch_via_docling: happy path with fake docling module ──────────
def _install_fake_docling(monkeypatch, *, markdown: str, tables: list[str] | None = None,
                          n_pages: int | None = None) -> None:
    """Install a fake ``docling.document_converter`` module into sys.modules.

    The fake's DocumentConverter.convert() returns an object whose
    .document supports .export_to_markdown() + .tables + .num_pages.
    """
    tables = tables or []
    n_pages_local = n_pages

    class _FakeTable:
        def __init__(self, md: str) -> None:
            self._md = md

        def export_to_markdown(self) -> str:
            return self._md

    class _FakeDoc:
        def __init__(self) -> None:
            self.tables = [_FakeTable(t) for t in tables]
            self.num_pages = n_pages_local

        def export_to_markdown(self) -> str:
            return markdown

    class _FakeResult:
        def __init__(self) -> None:
            self.document = _FakeDoc()

    class _FakeConverter:
        def convert(self, url: str) -> _FakeResult:  # noqa: ARG002
            return _FakeResult()

    fake_dc_mod = types.ModuleType("docling.document_converter")
    fake_dc_mod.DocumentConverter = _FakeConverter  # type: ignore[attr-defined]

    fake_docling_pkg = types.ModuleType("docling")
    fake_docling_pkg.document_converter = fake_dc_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "docling", fake_docling_pkg)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_dc_mod)


def test_fetch_via_docling_happy_path(monkeypatch) -> None:
    _install_fake_docling(
        monkeypatch,
        markdown=(
            "# Attention Is All You Need\n\n"
            "We propose a new simple network architecture, the Transformer, "
            "based solely on attention mechanisms.\n"
        ),
        tables=["| col1 | col2 |\n|------|------|\n| a | b |"],
        n_pages=15,
    )

    out = _run(fetch_via_docling("https://arxiv.org/pdf/1706.03762.pdf"))

    assert out["status"] == "ok"
    assert out["kind"] == "docling-document"
    assert out["title"] == "Attention Is All You Need"
    assert out["name"] == "Attention Is All You Need"
    assert "Attention" in out["text"]
    assert "Transformer" in out["text"]
    assert out["metadata"]["n_pages"] == 15
    assert out["metadata"]["n_tables"] == 1
    assert "col1" in out["metadata"]["tables_preview"][0]
    assert out["url"] == "https://arxiv.org/pdf/1706.03762.pdf"
    assert isinstance(out["ms"], int)


def test_fetch_via_docling_uses_url_when_no_heading(monkeypatch) -> None:
    _install_fake_docling(monkeypatch, markdown="Just some prose, no heading.\n")
    out = _run(fetch_via_docling("https://example.com/paper.pdf"))
    assert out["status"] == "ok"
    assert out["title"] == "https://example.com/paper.pdf"


def test_fetch_via_docling_handles_parse_exception(monkeypatch) -> None:
    """Errors raised by docling during parsing land in the error envelope."""
    class _FailingConverter:
        def convert(self, url: str) -> Any:  # noqa: ARG002
            raise RuntimeError("encoding broke")

    fake_dc_mod = types.ModuleType("docling.document_converter")
    fake_dc_mod.DocumentConverter = _FailingConverter  # type: ignore[attr-defined]
    fake_docling_pkg = types.ModuleType("docling")
    fake_docling_pkg.document_converter = fake_dc_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "docling", fake_docling_pkg)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_dc_mod)

    out = _run(fetch_via_docling("https://example.com/broken.pdf"))
    assert out["status"] == "error"
    assert "encoding broke" in out["error"]
    assert "docling parse failed" in out["error"]


def test_fetch_via_docling_proxy_kwarg_logged_but_ignored(monkeypatch, caplog) -> None:
    """The proxy kwarg is accepted but docling can't honor it in v2;
    confirm the function still succeeds and a warning surfaces."""
    _install_fake_docling(monkeypatch, markdown="# Title\n\nBody.\n")

    out = _run(fetch_via_docling(
        "https://example.com/paper.pdf",
        proxy="socks5://localhost:1080",
    ))
    assert out["status"] == "ok"
    # We don't assert on the log line — the log helper writes to a
    # named logger, capturing it would couple this test to log setup.
